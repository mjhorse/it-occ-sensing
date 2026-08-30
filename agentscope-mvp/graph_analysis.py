#!/usr/bin/env python3
"""UModel-backed graph queries for IT OCC app dependency analysis.

Topology must come from the persisted UModel workspace only. The remaining
SQLite helper is limited to historical metric baselines; it is not a topology
fallback.
"""
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

# Dynamic topology fallback intentionally removed: UModel is the only topology source.

def _umodel_dependency_neighborhood(appid: str, max_depth: int = 2, app_name: str | None = None) -> Dict[str, Any]:
    """Read topology exclusively from UModel.

    IT OCC topology has a single source of truth: UModel.  Do not synthesize
    dynamic appid nodes or silently fall back to SQLite when UModel is missing.
    A missing UModel service/workspace/appid is a data quality error and must
    fail fast so every page keeps using the same persisted topology facts.
    """
    from umodel_topology_provider import UModelTopologyProvider

    provider = UModelTopologyProvider(
        addr=os.environ.get("UMODEL_ADDR", "http://localhost:18080"),
        workspace=os.environ.get("UMODEL_WORKSPACE", "itocc-current"),
    )
    return provider.dependency_neighborhood(appid, max_depth=max_depth, app_name=app_name)


def dependency_neighborhood(appid: str, max_depth: int = 2, app_name: str | None = None) -> Dict[str, Any]:
    provider = os.environ.get("TOPOLOGY_PROVIDER", "umodel").strip().lower()
    if provider != "umodel":
        raise RuntimeError(
            f"TOPOLOGY_PROVIDER={provider!r} is disabled for IT OCC topology. "
            "All topology data must come from the persisted UModel workspace; "
            "no SQLite/dynamic fallback is allowed."
        )
    return _umodel_dependency_neighborhood(appid, max_depth=max_depth, app_name=app_name)


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

    # Downstream clues are still read from the same UModel graph; they are not generated fallback topology.
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
