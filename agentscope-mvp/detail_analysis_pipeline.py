#!/usr/bin/env python3
"""AgentScope-style detail analysis pipeline for one focus event."""
import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List
from graph_analysis import same_time_baseline, topology_reasoning

BASE = Path(__file__).parent

TYPE_LABEL = {
    "change": "变更",
    "alert": "告警",
    "sd": "SD单",
    "voice_feedback": "心声舆情",
    "event_ticket": "事件单",
    "warning": "预警",
}

@dataclass
class DetailStageResult:
    name: str
    status: str
    summary: str


def parse_time(v: Any) -> datetime:
    """Parse both ISO strings and browser-local zh-CN GMT+8 display strings."""
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone(timedelta(hours=8))).replace(tzinfo=None)
    s = str(v or "").strip()
    if not s:
        return datetime.fromtimestamp(0, tz=timezone(timedelta(hours=8))).replace(tzinfo=None)
    if s.endswith(" GMT+8"):
        s = s[:-6].strip()
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def event_time_value(event: Dict[str, Any]) -> Any:
    return event.get("event_time_sec") if event.get("event_time_sec") is not None else event.get("event_time")


def minutes_delta(t: Any, focus: Any) -> int:
    return round((parse_time(t) - parse_time(focus)).total_seconds() / 60)


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


class ContextCollectorAgent:
    name = "ContextCollectorAgent"

    def run(self, state: Dict[str, Any]) -> DetailStageResult:
        req = load_json(BASE / state["request"])
        state["request_obj"] = req
        focus_time = event_time_value(req["focus_event"])
        window = req.get("lookback_window_minutes", 10)
        related = []
        for event in req.get("discrete_events", []):
            delta = minutes_delta(event_time_value(event), focus_time)
            if abs(delta) <= window:
                row = dict(event)
                row["delta_min"] = delta
                related.append(row)
        state["related_events"] = sorted(related, key=lambda x: (abs(x["delta_min"]), event_time_value(x)))
        metrics = req.get("metrics_window", {})
        current_metrics = {
            "p95_latency_ms": metrics.get("p95_latency_ms", {}).get("latest", 0),
            "error_rate": metrics.get("error_rate", {}).get("latest", 0),
            "request_count": metrics.get("request_count", {}).get("latest", 0),
        }
        state["current_metrics"] = current_metrics
        state["same_time_baseline"] = same_time_baseline(req.get("appid"), current_metrics)
        msg = f"收集到 {len(related)} 个{window}分钟内关联离散event，并完成最近几天同时间段对比。" if related else f"{window}分钟内无附近离散event；已记录为无，并完成最近几天同时间段对比。"
        return DetailStageResult(self.name, "ok", msg)


class RuleExperienceAgent:
    name = "RuleExperienceAgent"

    def run(self, state: Dict[str, Any]) -> DetailStageResult:
        req = state["request_obj"]
        metrics = req.get("metrics_window", {})
        related = state["related_events"]
        has_change = any(e["event_type"] == "change" for e in related)
        has_user = any(e["event_type"] in ["sd", "voice_feedback"] for e in related)
        has_alert = any(e["event_type"] == "alert" for e in related)
        has_ticket = any(e["event_type"] == "event_ticket" for e in related)
        latency = metrics.get("p95_latency_ms", {})
        matched = []
        if has_change and has_user and latency.get("breach_points", 0) >= 2:
            matched.append({
                "experience_id": "EXP-CHANGE-LATENCY-001",
                "title": "变更后延迟升高 + 用户侧反馈",
                "why": "变更、延迟升高、用户反馈在同一时间窗口内出现。",
            })
        if has_alert and has_ticket:
            matched.append({
                "experience_id": "EXP-ALERT-INCIDENT-LINK-001",
                "title": "告警由事件单承接",
                "why": "告警仍为处理中，且窗口内存在处理中的事件单。",
            })
        state["rule_result"] = {
            "has_change": has_change,
            "has_user": has_user,
            "has_alert": has_alert,
            "has_ticket": has_ticket,
            "matched_experience_rules": matched,
        }
        return DetailStageResult(self.name, "ok", f"命中 {len(matched)} 条详情经验规则。")


class LLMInterpretationAgent:
    name = "LLMInterpretationAgent"

    def run(self, state: Dict[str, Any]) -> DetailStageResult:
        req = state["request_obj"]
        focus = req["focus_event"]
        related = state["related_events"]
        # Deterministic semantic pass for demo mode; it preserves the schema without pretending to be an LLM.
        interpretations = []
        for e in related:
            text = e.get("description", "")
            relevance = "high" if any(w in text for w in ["慢", "无响应", "超时", "等待", "升高", "处理中"]) else "medium"
            interpretations.append({
                "event_id": e.get("event_id"),
                "event_type": e.get("event_type"),
                "fault_relevance": relevance,
                "user_symptom_or_fact": text,
                "semantic_reason": "该描述与当前关注信号在时间窗口内接近，且语义上指向性能下降、用户等待或处置承接。",
            })
        state["llm_interpretation"] = {
            "focus_event_summary": focus.get("description"),
            "semantic_event_interpretation": interpretations,
            "llm_note": "当前为规则化语义整理；真实 AgentScope LLM Agent 可基于此 schema 进一步识别同义表达、反证和影响范围。",
        }
        return DetailStageResult(self.name, "ok", f"完成 {len(interpretations)} 个离散event的语义解释。")


def _probability_from_score(score: float) -> int:
    return max(35, min(92, int(round(score))))

def event_lane_counts(events: List[Dict[str, Any]]) -> Dict[str, int]:
    labels = {
        "warning": "预警", "alert": "告警", "sd": "SD单", "voice_feedback": "心声舆情",
        "forum": "论坛/舆情", "event_ticket": "事件单", "change": "变更", "metric_anomaly": "指标异常",
        "topology_call_impact": "调用影响",
    }
    counts: Dict[str, int] = {}
    for e in events or []:
        lane = labels.get(e.get("event_type"), e.get("event_type") or "未知")
        counts[lane] = counts.get(lane, 0) + 1
    return counts

def appid_level_impact_reasoning(req: Dict[str, Any], related_events: List[Dict[str, Any]], current_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Build appid-level correlation topology from UI-provided app signals.

    This intentionally avoids internal components. Nodes are appids only; edges
    represent inferred appid-to-appid impact based on call/dependency assumption
    plus metric/event evidence around the same time window.
    """
    focus_appid = req.get("appid")
    focus_name = req.get("app_name") or focus_appid
    signals = (req.get("topology_context") or {}).get("related_app_signals") or []
    has_focus_alert = any(e.get("event_type") in ["warning", "alert"] for e in related_events)
    has_focus_change = any(e.get("event_type") == "change" for e in related_events)
    focus_event_text = " ".join(e.get("description", "") for e in related_events)
    nodes = {focus_appid: {"appid": focus_appid, "name": focus_name, "kind": "预警应用", "lane_event_counts": event_lane_counts(related_events)}}
    inbound = []
    node_event_map: Dict[str, List[Dict[str, Any]]] = {
        focus_appid: [{
            "event_id": e.get("event_id"), "event_type": e.get("event_type"), "event_time": e.get("event_time"),
            "severity": e.get("severity"), "status": e.get("status"), "description": e.get("description"),
            "relation_to_focus": "当前预警 appid 的10分钟内附近离散event",
        } for e in related_events]
    }
    for sig in signals:
        src = sig.get("appid")
        if not src or src == focus_appid:
            continue
        evs = sig.get("events") or []
        metrics = sig.get("metrics") or {}
        evidence = []
        score = 35.0
        if has_focus_alert:
            score += 15; evidence.append(f"{focus_name} 同窗口存在预警/告警")
        if metrics.get("error_rate_ratio", 0) >= 1.5:
            score += 18; evidence.append(f"{sig.get('app_name') or src} 错误率较基线/前序升高 {metrics.get('error_rate_ratio')} 倍")
        if metrics.get("p95_latency_ratio", 0) >= 1.4:
            score += 16; evidence.append(f"{sig.get('app_name') or src} 访问耗时较前序升高 {metrics.get('p95_latency_ratio')} 倍")
        if evs:
            score += 10; evidence.append(f"{sig.get('app_name') or src} 10分钟内存在 {len(evs)} 条离散event")
        if has_focus_change:
            score += 8; evidence.append(f"{focus_name} 告警前后窗口存在变更，建议作为候选诱因排查")
        if not evidence:
            continue
        probability = _probability_from_score(score)
        nodes[src] = {"appid": src, "name": sig.get("app_name") or src, "kind": "关联应用", "lane_event_counts": event_lane_counts(evs)}
        inbound.append({
            "src": src, "dst": focus_appid, "relation": "调用", "relation_label": "调用", "weight": round(probability / 100, 2),
            "description": "appid级影响推理：调用方指标/event 与被调用方预警同窗口异常",
            "depth": 1, "path": f"{src}->{focus_appid}", "src_name": sig.get("app_name") or src, "dst_name": focus_name,
            "src_kind": "appid", "dst_kind": "appid", "suspicion_score": round(probability / 100, 2),
            "impact_probability": probability, "reason": evidence,
        })
        node_event_map[src] = [{
            "event_id": e.get("event_id"), "event_type": e.get("event_type"), "event_time": e.get("event_time"),
            "severity": e.get("severity"), "status": e.get("status"), "description": e.get("description"),
            "relation_to_focus": "关联 appid 的10分钟内附近离散event",
        } for e in evs] or [{
            "event_id": f"METRIC-{src}", "event_type": "metric_anomaly", "event_time": req.get("focus_event", {}).get("event_time"),
            "severity": "指标异常", "status": "待确认", "description": "同窗口指标升高但暂无离散event。",
            "relation_to_focus": "关联 appid 的指标侧证据",
        }]
    inbound = sorted(inbound, key=lambda x: x.get("impact_probability", 0), reverse=True)[:8]
    opinion = "；".join([f"{x['src_name']} → {x['dst_name']}（{x['relation_label']}，推断{int(x['impact_probability'])}%概率受影响）" for x in inbound])
    if has_focus_change:
        opinion = (opinion + "；" if opinion else "") + f"{focus_name} 预警前后存在变更，建议优先核查变更影响范围。"
    return {
        "graph": {
            "focus_node": {"appid": focus_appid, "name": focus_name, "kind": "appid", "domain": "当前预警应用", "description": "来自预警界面的当前 appid"},
            "inbound_paths": inbound,
            "upstream_edges": inbound,
            "downstream_paths": [],
            "graph_view": "appid_level_inbound_impact",
            "graph_store": "ui_payload_related_app_signals",
        },
        "topology_view": "appid级关联：调用方 appid → 预警 appid",
        "suspected_dependency_paths": inbound,
        "suspected_inbound_callers": inbound,
        "downstream_root_cause_clues": [],
        "topology_opinion": opinion or "10分钟窗口内暂未发现其他 appid 的指标/event 关联证据。",
        "node_event_map": node_event_map,
        "scope_note": "仅展示 appid 级关联，不展示 appid 内部组件。",
    }


class TopologyDependencyAgent:
    name = "TopologyDependencyAgent"

    def run(self, state: Dict[str, Any]) -> DetailStageResult:
        req = state["request_obj"]
        appid = req.get("appid")
        # Topology is UModel-only.  UI-provided same-window related_app_signals
        # may be used as non-topology evidence elsewhere, but must never become
        # topology edges.  If UModel is unavailable or the appid is missing, fail
        # fast instead of synthesizing a second graph.
        result = topology_reasoning(appid, state.get("related_events", []), state.get("current_metrics", {}), app_name=req.get("app_name"))
        result = attach_topology_node_events(result, state.get("related_events", []), appid)
        state["topology_dependency_analysis"] = result
        graph_store = (result.get("graph") or {}).get("graph_store", "")
        if str(graph_store).startswith("umodel:"):
            source = "UModel拓扑"
        else:
            raise RuntimeError(f"TopologyDependencyAgent rejected non-UModel graph_store: {graph_store}")
        return DetailStageResult(self.name, "ok", f"基于{source}识别 {len(result.get('suspected_inbound_callers') or result.get('suspected_dependency_paths', []))} 个关联 appid。")




def attach_topology_node_events(topology: Dict[str, Any], related_events: List[Dict[str, Any]], focus_appid: str) -> Dict[str, Any]:
    """Attach concise event cards to every appid shown in topology.

    The sample request only carries focus-app events, so caller nodes get an
    explicit derived caller-impact event tied to the dependency edge rather than
    pretending there was a raw alert/SD on that caller appid.
    """
    node_events: Dict[str, List[Dict[str, Any]]] = {}
    focus_events = []
    for e in related_events:
        focus_events.append({
            "event_id": e.get("event_id"),
            "event_type": e.get("event_type"),
            "event_time": e.get("event_time"),
            "severity": e.get("severity"),
            "status": e.get("status"),
            "description": e.get("description"),
            "relation_to_focus": "中心预警 appid 的原始关联 event",
        })
    node_events[focus_appid] = focus_events[:6]

    topology_evidence: Dict[str, List[Dict[str, Any]]] = {}
    for edge in topology.get("suspected_inbound_callers") or topology.get("suspected_dependency_paths") or []:
        src = edge.get("src")
        if not src:
            continue
        topology_evidence.setdefault(src, []).append({
            "evidence_type": "topology_call_impact",
            "evidence_id": f"TOPO-{src}",
            "description": f"{edge.get('src_name') or src} 调用/依赖中心预警 appid，关系：{edge.get('relation_label') or edge.get('relation') or '调用'}；{edge.get('description') or '需确认调用侧体验与错误率'}",
            "relation_to_focus": "调用方 → 中心预警 appid，被调用关系证据",
        })

    for edge in topology.get("downstream_root_cause_clues") or []:
        dst = edge.get("dst")
        if not dst:
            continue
        topology_evidence.setdefault(dst, []).append({
            "evidence_type": "downstream_root_cause_clue",
            "evidence_id": f"ROOT-{dst}",
            "description": f"{edge.get('dst_name') or dst} 是中心 appid 下游依赖，{edge.get('description') or '作为根因辅助排查线索'}",
            "relation_to_focus": "辅助根因线索，不作为主图被调用语义",
        })

    topology["node_event_map"] = node_events
    topology["node_topology_evidence"] = topology_evidence
    return topology


class CorrelationNarratorAgent:
    name = "CorrelationNarratorAgent"

    def run(self, state: Dict[str, Any]) -> DetailStageResult:
        req = state["request_obj"]
        rule = state["rule_result"]
        related = state["related_events"]
        factors = []
        if rule["has_alert"]:
            factors.append("存在处理中告警")
        if rule["has_user"]:
            factors.append("存在用户侧反馈")
        if rule["has_change"]:
            factors.append("前序存在变更")
        if rule["has_ticket"]:
            factors.append("已有事件单承接")
        baseline = state.get("same_time_baseline", {})
        topology = state.get("topology_dependency_analysis", {})
        if baseline.get("stats", {}).get("p95_latency_ms", {}).get("z_score", 0) >= 2:
            factors.append("显著高于最近几天同时间段基线")
        if topology.get("suspected_inbound_callers") or topology.get("suspected_dependency_paths"):
            factors.append("拓扑中存在调用当前预警 appid 的高疑似上游调用方")
        level = "strong" if len(factors) >= 3 else "medium" if factors else "none"
        timeline = []
        focus_time = event_time_value(req["focus_event"])
        for e in sorted(related, key=lambda x: x["event_time"]):
            delta = minutes_delta(e["event_time"], focus_time)
            timeline.append({
                "time": f"T{delta:+d}m" if delta else "T",
                "type": e["event_type"],
                "type_label": TYPE_LABEL.get(e["event_type"], e["event_type"]),
                "description": e.get("description"),
                "interpretation": _interpret_event(e),
            })
        missing = list(req.get("topology_context", {}).get("missing_inputs", []))
        timeline_note = f"已按预警时间前后{req.get('lookback_window_minutes', 10)}分钟筛选附近离散event；无匹配则显示‘无’。"
        response = {
            "schema_version": "it_occ_agent_detail_view.v1",
            "appid": req.get("appid"),
            "focus_event_id": req["focus_event"].get("event_id"),
            "correlation_level": level,
            "correlation_label": {"strong": "关联较强", "medium": "存在关联", "weak": "关联较弱", "none": "暂未关联"}.get(level, "暂未关联"),
            "correlation_summary": f"最近{req.get('lookback_window_minutes', 10)}分钟内，{ '、'.join(factors) if factors else '无附近离散event或明显关联信号' }，建议作为同一异常链路继续核对。",
            "timeline_reasoning": timeline,
            "timeline_note": timeline_note,
            "nearby_event_window_minutes": req.get("lookback_window_minutes", 10),
            "same_time_baseline": baseline,
            "topology_dependency_analysis": topology,
            "matched_experience_rules": rule["matched_experience_rules"],
            "llm_semantic_interpretation": state["llm_interpretation"],
            "missing_inputs": missing,
            "suggested_next_actions": ["查看appid级关联拓扑", "确认上游调用方影响范围", "确认事件单是否仍处理中", "核对变更影响范围"],
        }
        state["response"] = response
        return DetailStageResult(self.name, "ok", f"生成详情关联解读：{response['correlation_label']}。")


def _interpret_event(e: Dict[str, Any]) -> str:
    typ = e.get("event_type")
    if typ == "change":
        return "变更发生在关注信号之前，可作为候选诱因。"
    if typ == "alert":
        return "告警处于处理中，说明结构化监控侧仍未闭环。"
    if typ in ["sd", "voice_feedback"]:
        return "用户侧描述与性能下降方向一致，可作为影响证据。"
    if typ == "event_ticket":
        return "事件单承接处置，需继续关注是否关闭及关闭原因。"
    return "作为关联离散event纳入判断。"


def run_pipeline(args: argparse.Namespace) -> Dict[str, Any]:
    state: Dict[str, Any] = {"request": args.request}
    stages = [ContextCollectorAgent(), RuleExperienceAgent(), LLMInterpretationAgent(), TopologyDependencyAgent(), CorrelationNarratorAgent()]
    results = [stage.run(state).__dict__ for stage in stages]
    out = BASE / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state["response"], ensure_ascii=False, indent=2))
    report = {"schema_version": "it_occ_detail_analysis_pipeline_report.v1", "stages": results, "output": args.output}
    (BASE / args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", default="inputs/sample_detail_request_v1.json")
    ap.add_argument("--output", default="outputs/sample_agent_detail_analysis_v1.json")
    ap.add_argument("--report", default="outputs/detail_analysis_pipeline_report_v1.json")
    args = ap.parse_args()
    print(json.dumps(run_pipeline(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
