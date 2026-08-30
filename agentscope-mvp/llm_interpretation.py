#!/usr/bin/env python3
"""Build LLM interpretation prompts for non-structured IT OCC sensing events.

This module does not call an external model by default. It creates the exact
inputs/outputs AgentScope LLM agents should use, so the deterministic rule layer
can be combined with LLM interpretation without making the local MVP depend on a
specific provider key.
"""
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

BASE = Path(__file__).parent

SYSTEM_POLICY = """你是 IT OCC 感知视算的规则解读助手。请结合结构化经验规则和非结构化离散event文本，输出可审计的规则解读。\n要求：\n1. event 是所有离散信息点的总称；事件单只是 event_ticket 类型。\n2. 不要编造不存在的事实；不确定就标注 missing_inputs。\n3. 告警状态只能是“处理中”或“已关闭”。\n4. 输出 JSON，不要输出 Markdown。\n"""


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def collect_unstructured_events(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    events.extend(candidate.get("current_snapshot", {}).get("events_nearby", []))
    for win_name, win in candidate.get("historical_windows", {}).items():
        if not isinstance(win, dict):
            continue
        for event in win.get("events", []):
            row = dict(event)
            row.setdefault("window", win_name)
            events.append(row)
    seen = set()
    dedup = []
    for event in events:
        key = (event.get("type"), event.get("time_offset_min"), event.get("title"), event.get("description"), event.get("content"))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(event)
    return dedup


def build_prompt(candidate: Dict[str, Any], rules: List[Dict[str, Any]], judgement: Dict[str, Any] | None) -> Dict[str, Any]:
    selected_rules = [
        {
            "experience_id": r.get("experience_id"),
            "title": r.get("title"),
            "positive_logic": r.get("positive_logic"),
            "negative_logic": r.get("negative_logic"),
            "evidence_requirements": r.get("evidence_requirements"),
            "counter_evidence": r.get("counter_evidence"),
        }
        for r in rules
    ]
    return {
        "system": SYSTEM_POLICY,
        "input": {
            "candidate": {
                "candidate_id": candidate.get("candidate_id"),
                "appid": candidate.get("appid"),
                "app_name": candidate.get("app_name"),
                "candidate_time": candidate.get("candidate_time"),
                "metrics": candidate.get("current_snapshot", {}).get("metrics", {}),
                "topology_context": candidate.get("topology_context", {}),
            },
            "unstructured_discrete_events": collect_unstructured_events(candidate),
            "experience_rules": selected_rules,
            "deterministic_judgement": judgement or {},
        },
        "expected_output_schema": {
            "semantic_event_interpretation": [
                {
                    "event_type": "voice_feedback|sd|change|alert|event_ticket|other",
                    "user_symptom": "用户/业务侧真实症状摘要",
                    "fault_relevance": "high|medium|low|unknown",
                    "mapped_rule_evidence": ["experience_id + evidence/counter_evidence"],
                    "reasoning_short": "简短原因",
                }
            ],
            "rule_interpretation": {
                "matched_experience_rules": ["experience_id"],
                "counter_evidence": ["反证"],
                "missing_inputs": ["缺失输入"],
                "confidence_adjustment_suggestion": "raise|lower|none",
                "confidence_adjustment_reason": "原因",
            },
            "user_facing_narrative": "面向用户的一段规则解读",
        },
    }


def fallback_interpretation(prompt: Dict[str, Any]) -> Dict[str, Any]:
    events = prompt["input"].get("unstructured_discrete_events", [])
    judgement = prompt["input"].get("deterministic_judgement", {})
    interpreted = []
    for event in events:
        text = event.get("title") or event.get("description") or event.get("content") or ""
        typ = event.get("type", "other")
        relevance = "high" if any(word in text for word in ["慢", "失败", "超时", "无响应", "卡住", "繁忙"]) else "medium" if typ in ["change", "event_ticket"] else "unknown"
        interpreted.append({
            "event_type": typ,
            "user_symptom": text,
            "fault_relevance": relevance,
            "mapped_rule_evidence": ["EXP-CHANGE-LATENCY-001:user_feedback/change" if typ in ["voice_feedback", "change"] else "unmapped"],
            "reasoning_short": "本地fallback仅做关键词和类型映射；真实泛化场景应交由LLM判断语义、同义表达和反证。",
        })
    return {
        "semantic_event_interpretation": interpreted,
        "rule_interpretation": {
            "matched_experience_rules": [r.get("experience_id") for r in judgement.get("experience_rule_evaluation", {}).get("matched_rules", [])],
            "counter_evidence": judgement.get("evidence_against", []),
            "missing_inputs": judgement.get("missing_inputs", []),
            "confidence_adjustment_suggestion": "none",
            "confidence_adjustment_reason": "未调用真实LLM，本地fallback不改写确定性规则置信度。",
        },
        "user_facing_narrative": "规则侧已给出结构化判读；非结构化文本需要由LLM进一步识别用户症状、影响范围、同义表达和反证，当前为本地fallback说明。",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", default="inputs/sample_candidate_v2.json")
    ap.add_argument("--rules", default="kb/experience-rules.jsonl")
    ap.add_argument("--judgement", default="outputs/sample_judgement_v2.json")
    ap.add_argument("--prompt-output", default="outputs/llm_rule_interpretation_prompt_v1.json")
    ap.add_argument("--fallback-output", default="outputs/llm_rule_interpretation_fallback_v1.json")
    args = ap.parse_args()
    candidate = load_json(BASE / args.candidate)
    rules = load_jsonl(BASE / args.rules)
    judgement_path = BASE / args.judgement
    judgement = load_json(judgement_path) if judgement_path.exists() else None
    prompt = build_prompt(candidate, rules, judgement)
    (BASE / args.prompt_output).write_text(json.dumps(prompt, ensure_ascii=False, indent=2))
    (BASE / args.fallback_output).write_text(json.dumps(fallback_interpretation(prompt), ensure_ascii=False, indent=2))
    print(BASE / args.prompt_output)
    print(BASE / args.fallback_output)


if __name__ == "__main__":
    main()
