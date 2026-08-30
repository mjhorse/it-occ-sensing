#!/usr/bin/env python3
"""SQLite-backed graph queries for IT OCC app dependency analysis."""
import json
import os
import sqlite3
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Dict, List

BASE = Path(__file__).parent
DB = BASE / "graph" / "it_occ_app_graph.sqlite"


def _con():
    return sqlite3.connect(DB)

RELATION_LABEL = {
    "sync_call": "同步调用",
    "api_call": "接口调用",
    "callback": "回跳调用",
    "cache_read": "读取缓存",
    "db_read_write": "数据库读写",
    "async_publish": "异步发布",
    "mq_publish": "消息发布",
    "gateway_route": "网关转发",
}

def relation_label(relation: str) -> str:
    return RELATION_LABEL.get(relation or "", relation or "调用")

def _with_relation_label(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        x = dict(r)
        original_relation = x.get("relation")
        label = relation_label(original_relation)
        x["relation"] = label
        x["relation_label"] = label
        out.append(x)
    return out

def _fallback_neighborhood(appid: str, app_name: str | None = None) -> Dict[str, Any]:
    parts = appid.split(".")
    prefix = ".".join(parts[:-1]) if len(parts) > 1 else appid
    module_name = app_name or appid
    caller_specs = [
        (f"{prefix}.web", "业务门户", "frontend_app", "同步调用", "页面入口调用当前预警 appid"),
        (f"{prefix}.mobile", "移动端", "mobile_app", "同步调用", "移动端入口调用当前预警 appid"),
        (f"{prefix}.open", "开放网关", "gateway", "接口调用", "外部/伙伴渠道调用当前预警 appid"),
        (f"{prefix}.job", "批处理任务", "job", "定时调用", "后台任务调用当前预警 appid"),
    ]
    inbound = []
    for idx, (src, suffix, kind, label, desc) in enumerate(caller_specs):
        inbound.append({
            "src": src, "dst": appid, "relation": label, "relation_label": label,
            "weight": round(0.78 - idx * 0.08, 2), "description": desc, "depth": 1,
            "path": f"{src}->{appid}", "src_name": f"{module_name}-{suffix}",
            "dst_name": module_name, "src_kind": kind, "dst_kind": "service",
        })
    downstream = [
        {"src": appid, "dst": f"{prefix}.db", "relation": "数据库读写", "relation_label": "数据库读写", "weight": 0.62, "description": "当前预警 appid 的数据读写依赖", "depth": 1, "path": f"{appid}->{prefix}.db", "src_name": module_name, "dst_name": f"{module_name}-数据库", "dst_kind": "database"},
        {"src": appid, "dst": f"{prefix}.cache", "relation": "读取缓存", "relation_label": "读取缓存", "weight": 0.58, "description": "当前预警 appid 的缓存读取依赖", "depth": 1, "path": f"{appid}->{prefix}.cache", "src_name": module_name, "dst_name": f"{module_name}-缓存", "dst_kind": "cache"},
    ]
    return {
        "focus_node": {"appid": appid, "name": module_name, "kind": "service", "domain": "当前预警应用", "description": "来自预警界面的当前 appid"},
        "downstream_paths": downstream,
        "upstream_edges": inbound,
        "inbound_paths": inbound,
        "graph_view": "inbound_callers_centered_on_warning_appid",
        "graph_store": "dynamic_from_warning_appid",
    }


def _umodel_dependency_neighborhood(appid: str, max_depth: int = 2, app_name: str | None = None) -> Dict[str, Any]:
    """Try UModel topology provider when explicitly enabled.

    Keeps SQLite/dynamic behavior as fallback so local demos still work without
    a running UModel service.
    """
    from umodel_topology_provider import UModelTopologyProvider

    provider = UModelTopologyProvider(
        addr=os.environ.get("UMODEL_ADDR", "http://localhost:8080"),
        workspace=os.environ.get("UMODEL_WORKSPACE", "itocc-demo"),
    )
    return provider.dependency_neighborhood(appid, max_depth=max_depth, app_name=app_name)


def _sqlite_dependency_neighborhood(appid: str, max_depth: int = 2, app_name: str | None = None) -> Dict[str, Any]:
    con = _con()
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        """
        WITH RECURSIVE walk(src, dst, relation, weight, description, depth, path) AS (
          SELECT src, dst, relation, weight, description, 1, src || '->' || dst
          FROM app_edges WHERE src = ?
          UNION ALL
          SELECT e.src, e.dst, e.relation, e.weight, e.description, w.depth + 1, w.path || '->' || e.dst
          FROM app_edges e JOIN walk w ON e.src = w.dst
          WHERE w.depth < ? AND instr(w.path, e.dst) = 0
        )
        SELECT w.*, sn.name src_name, dn.name dst_name, dn.kind dst_kind
        FROM walk w
        LEFT JOIN app_nodes sn ON sn.appid = w.src
        LEFT JOIN app_nodes dn ON dn.appid = w.dst
        ORDER BY depth, weight DESC
        """,
        (appid, max_depth),
    ).fetchall()
    upstream = cur.execute(
        """
        SELECT e.*, sn.name src_name, dn.name dst_name
        FROM app_edges e
        LEFT JOIN app_nodes sn ON sn.appid=e.src
        LEFT JOIN app_nodes dn ON dn.appid=e.dst
        WHERE e.dst=? ORDER BY e.weight DESC
        """,
        (appid,),
    ).fetchall()
    inbound_rows = cur.execute(
        """
        WITH RECURSIVE walk(src, dst, relation, weight, description, depth, path) AS (
          SELECT src, dst, relation, weight, description, 1, src || '->' || dst
          FROM app_edges WHERE dst = ?
          UNION ALL
          SELECT e.src, e.dst, e.relation, e.weight, e.description, w.depth + 1, e.src || '->' || w.path
          FROM app_edges e JOIN walk w ON e.dst = w.src
          WHERE w.depth < ? AND instr(w.path, e.src) = 0
        )
        SELECT w.*, sn.name src_name, dn.name dst_name, sn.kind src_kind, dn.kind dst_kind
        FROM walk w
        LEFT JOIN app_nodes sn ON sn.appid = w.src
        LEFT JOIN app_nodes dn ON dn.appid = w.dst
        ORDER BY depth, weight DESC
        """,
        (appid, max_depth),
    ).fetchall()
    node = cur.execute("SELECT * FROM app_nodes WHERE appid=?", (appid,)).fetchone()
    con.close()
    if not node:
        return _fallback_neighborhood(appid, app_name)
    focus = dict(node)
    if app_name:
        focus["name"] = app_name
    return {
        "focus_node": focus,
        "downstream_paths": _with_relation_label([dict(r) for r in rows]),
        "upstream_edges": _with_relation_label([dict(r) for r in upstream]),
        "inbound_paths": _with_relation_label([dict(r) for r in inbound_rows]),
        "graph_view": "inbound_callers_centered_on_warning_appid",
        "graph_store": str(DB),
    }


def dependency_neighborhood(appid: str, max_depth: int = 2, app_name: str | None = None) -> Dict[str, Any]:
    provider = os.environ.get("TOPOLOGY_PROVIDER", "sqlite").strip().lower()
    if provider == "umodel":
        try:
            return _umodel_dependency_neighborhood(appid, max_depth=max_depth, app_name=app_name)
        except Exception as exc:
            fallback = _sqlite_dependency_neighborhood(appid, max_depth=max_depth, app_name=app_name)
            fallback["graph_store_fallback_reason"] = f"umodel_unavailable: {exc}"
            fallback["requested_graph_store"] = "umodel"
            return fallback
    return _sqlite_dependency_neighborhood(appid, max_depth=max_depth, app_name=app_name)


def same_time_baseline(appid: str, current: Dict[str, Any], time_bucket: str = "10:05-10:20") -> Dict[str, Any]:
    con = _con()
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM historical_metrics WHERE appid=? AND time_bucket=? ORDER BY day_offset",
        (appid, time_bucket),
    ).fetchall()]
    con.close()
    if not rows:
        return {"time_bucket": time_bucket, "samples": [], "opinion": "无历史同时间段样本，无法比较。"}
    def metric_stat(name: str, current_value: float):
        vals = [float(r[name]) for r in rows]
        avg = mean(vals)
        sd = pstdev(vals) or 1.0
        z = (current_value - avg) / sd
        return {"current": current_value, "baseline_avg": round(avg, 3), "baseline_std": round(sd, 3), "z_score": round(z, 2), "ratio": round(current_value / avg, 2) if avg else None}
    stats = {}
    if "p95_latency_ms" in current:
        stats["p95_latency_ms"] = metric_stat("p95_latency_ms", float(current["p95_latency_ms"]))
    if "error_rate" in current:
        stats["error_rate"] = metric_stat("error_rate", float(current["error_rate"]))
    if "request_count" in current:
        stats["request_count"] = metric_stat("request_count", float(current["request_count"]))
    opinions = []
    p95 = stats.get("p95_latency_ms")
    if p95 and p95["z_score"] >= 3:
        opinions.append(f"P95 延迟显著高于最近7天同时间段均值，约为基线 {p95['ratio']} 倍。")
    elif p95 and p95["z_score"] >= 1.5:
        opinions.append("P95 延迟高于历史同时间段正常波动，需要关注。")
    er = stats.get("error_rate")
    if er and er["z_score"] >= 2:
        opinions.append(f"错误率也高于历史同时间段，约为基线 {er['ratio']} 倍。")
    elif er:
        opinions.append("错误率未显著偏离历史同时间段，当前更像性能退化而非大量失败。")
    return {"time_bucket": time_bucket, "samples": rows, "stats": stats, "opinion": " ".join(opinions) or "当前指标未明显偏离历史同时间段。"}


def topology_reasoning(appid: str, events: List[Dict[str, Any]], current_metrics: Dict[str, Any], app_name: str | None = None) -> Dict[str, Any]:
    graph = dependency_neighborhood(appid, 2, app_name=app_name)
    suspicious = []
    text = " ".join(e.get("description", "") for e in events)

    # Primary view for warnings: who calls / depends on the warning appid.
    # Edges keep their real call direction: caller(src) -> warning appid(dst).
    inbound_paths = graph.get("inbound_paths") or graph.get("upstream_edges") or []
    for p in inbound_paths:
        score = float(p.get("weight") or 0)
        reason = ["被调用关系：该节点调用/依赖当前预警 appid"]
        if "合同" in text and "contract" in p.get("src", ""):
            score += 0.18; reason.append("事件描述包含合同/回跳线索，调用方业务相关")
        if "报价" in text and "quote" in p.get("src", ""):
            score += 0.16; reason.append("事件描述包含报价链路线索，调用方业务相关")
        if current_metrics.get("p95_latency_ms", 0):
            score += 0.06; reason.append("中心 appid 性能退化可能被上游调用方感知")
        if score >= 0.45:
            suspicious.append({**p, "suspicion_score": round(score, 2), "reason": reason})

    # Keep a small downstream fallback for root-cause clues, but do not use it as the main topology semantics.
    downstream_clues = []
    for p in graph.get("downstream_paths", []):
        score = float(p.get("weight") or 0)
        reason = []
        if "缓存" in text and "cache" in p.get("dst", ""):
            score += 0.25; reason.append("事件描述包含缓存相关线索")
        if score >= 0.8:
            downstream_clues.append({**p, "suspicion_score": round(score, 2), "reason": reason or ["中心 appid 下游依赖，作为根因排查线索"]})

    suspicious = sorted(suspicious, key=lambda x: x["suspicion_score"], reverse=True)[:6]
    opinion = "；".join([f"{x.get('src_name') or x['src']} → {x.get('dst_name') or x['dst']}（{x.get('relation_label') or relation_label(x.get('relation'))}）" for x in suspicious])
    return {
        "graph": graph,
        "topology_view": "被调用关系：调用方 → 中心预警 appid",
        "suspected_dependency_paths": suspicious,
        "suspected_inbound_callers": suspicious,
        "downstream_root_cause_clues": downstream_clues[:3],
        "topology_opinion": opinion or "拓扑中暂未发现直接调用当前预警 appid 的高疑似上游调用方。",
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("appid", default="com.sale.quote.center", nargs="?")
    args = ap.parse_args()
    result = {
        "dependency_neighborhood": dependency_neighborhood(args.appid),
        "same_time_baseline": same_time_baseline(args.appid, {"p95_latency_ms": 2066, "error_rate": 0.047, "request_count": 2520}),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
