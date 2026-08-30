#!/usr/bin/env python3
"""
AgentScope-style MVP pipeline for IT OCC sensing judgement.

This file keeps the AgentScope boundary explicit while remaining runnable in the
current local environment where the `agentscope` package may not be installed.
When AgentScope is available, the same stage boundaries can be wrapped as real
AgentScope agents; today they are deterministic local agents for demo/testing.
"""
import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

BASE = Path(__file__).parent


@dataclass
class StageResult:
    name: str
    status: str
    summary: str
    artifact: str | None = None


class LocalAgent:
    def __init__(self, name: str):
        self.name = name

    def run(self, state: Dict[str, Any]) -> StageResult:  # pragma: no cover - abstract-ish
        raise NotImplementedError


class CandidateReaderAgent(LocalAgent):
    def run(self, state: Dict[str, Any]) -> StageResult:
        candidate_path = BASE / state["candidate"]
        candidate = json.loads(candidate_path.read_text())
        state["candidate_obj"] = candidate
        event_count = len(candidate.get("current_snapshot", {}).get("events_nearby", []))
        return StageResult(
            self.name,
            "ok",
            f"读取候选预警 {candidate.get('candidate_id')}，当前快照包含 {event_count} 个离散event。",
            str(candidate_path.relative_to(BASE)),
        )


class RuleEvaluatorAgent(LocalAgent):
    def run(self, state: Dict[str, Any]) -> StageResult:
        output = state["judgement_output"]
        cmd = [sys.executable, str(BASE / "rule_evaluator.py"), "--candidate", state["candidate"], "--rules", state["rules"], "--output", output]
        subprocess.run(cmd, cwd=BASE, check=True)
        judgement = json.loads((BASE / output).read_text())
        state["judgement_obj"] = judgement
        matched = judgement.get("experience_rule_evaluation", {}).get("matched_rules", [])
        return StageResult(
            self.name,
            "ok",
            f"完成规则判读，置信度 {judgement.get('confidence_label')}({judgement.get('confidence_score')})，命中 {len(matched)} 条经验规则。",
            output,
        )


class LLMInterpretationAgent(LocalAgent):
    def run(self, state: Dict[str, Any]) -> StageResult:
        prompt_output = state.get("llm_prompt_output", "outputs/llm_rule_interpretation_prompt_v1.json")
        fallback_output = state.get("llm_fallback_output", "outputs/llm_rule_interpretation_fallback_v1.json")
        cmd = [
            sys.executable,
            str(BASE / "llm_interpretation.py"),
            "--candidate",
            state["candidate"],
            "--rules",
            state["rules"],
            "--judgement",
            state["judgement_output"],
            "--prompt-output",
            prompt_output,
            "--fallback-output",
            fallback_output,
        ]
        subprocess.run(cmd, cwd=BASE, check=True)
        state["llm_prompt_output"] = prompt_output
        state["llm_fallback_output"] = fallback_output
        state["llm_interpretation_obj"] = json.loads((BASE / fallback_output).read_text())
        return StageResult(
            self.name,
            "ok",
            "已生成 LLM 规则解读 prompt，并用本地fallback产出语义解释；真实LLM接入后用于泛化非结构化event。",
            prompt_output,
        )


class EvidenceNarratorAgent(LocalAgent):
    def run(self, state: Dict[str, Any]) -> StageResult:
        candidate = state["candidate_obj"]
        judgement = state["judgement_obj"]
        events: List[Dict[str, Any]] = []
        events.extend(candidate.get("current_snapshot", {}).get("events_nearby", []))
        for window in candidate.get("historical_windows", {}).values():
            events.extend(window.get("events", []) if isinstance(window, dict) else [])
        unique = []
        seen = set()
        for event in events:
            key = (event.get("type"), event.get("time_offset_min"), event.get("title"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(event)
        narrative = {
            "candidate_id": candidate.get("candidate_id"),
            "appid": candidate.get("appid"),
            "concept_note": "event 是所有离散信息点的总称；事件单 event_ticket 只是 event 的一种。",
            "user_readable_evidence": [
                {
                    "event_type": e.get("type"),
                    "time_offset_min": e.get("time_offset_min"),
                    "description": e.get("title") or e.get("description") or e.get("content"),
                }
                for e in unique
            ],
            "judgement_summary": {
                "label": judgement.get("confidence_label"),
                "score": judgement.get("confidence_score"),
                "recommended_actions": judgement.get("recommended_actions", []),
            },
            "llm_semantic_interpretation": state.get("llm_interpretation_obj", {}),
        }
        out = BASE / state["narrative_output"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(narrative, ensure_ascii=False, indent=2))
        return StageResult(self.name, "ok", f"生成面向用户的证据说明，覆盖 {len(unique)} 个离散event。", state["narrative_output"])


class FeedbackLearningAgent(LocalAgent):
    def run(self, state: Dict[str, Any]) -> StageResult:
        if not state.get("feedback"):
            return StageResult(self.name, "skipped", "未提供人工反馈，跳过经验规则统计回写。")
        out = state["feedback_output"]
        cmd = [sys.executable, str(BASE / "apply_feedback.py"), "--rules", state["rules"], "--feedback", state["feedback"], "--out", out]
        subprocess.run(cmd, cwd=BASE, check=True)
        return StageResult(self.name, "ok", "已根据人工反馈更新经验规则统计。", out)


def run_pipeline(args: argparse.Namespace) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "candidate": args.candidate,
        "rules": args.rules,
        "feedback": args.feedback,
        "judgement_output": args.judgement_output,
        "narrative_output": args.narrative_output,
        "feedback_output": args.feedback_output,
        "llm_prompt_output": args.llm_prompt_output,
        "llm_fallback_output": args.llm_fallback_output,
    }
    stages: List[LocalAgent] = [
        CandidateReaderAgent("CandidateReaderAgent"),
        RuleEvaluatorAgent("RuleEvaluatorAgent"),
        LLMInterpretationAgent("LLMInterpretationAgent"),
        EvidenceNarratorAgent("EvidenceNarratorAgent"),
        FeedbackLearningAgent("FeedbackLearningAgent"),
    ]
    results = [stage.run(state).__dict__ for stage in stages]
    report = {
        "schema_version": "it_occ_sensing_agentscope_pipeline_report.v1",
        "runtime": "local_agent_fallback",
        "agentscope_package_available": _agentscope_available(),
        "stages": results,
    }
    report_path = BASE / args.report_output
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _agentscope_available() -> bool:
    try:
        import agentscope  # noqa: F401
        return True
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", default="inputs/sample_candidate_v2.json")
    ap.add_argument("--rules", default="kb/experience-rules.jsonl")
    ap.add_argument("--feedback", default="")
    ap.add_argument("--judgement-output", default="outputs/sample_judgement_v2.json")
    ap.add_argument("--narrative-output", default="outputs/sample_evidence_narrative_v1.json")
    ap.add_argument("--feedback-output", default="kb/experience-rules.pipeline-updated.jsonl")
    ap.add_argument("--llm-prompt-output", default="outputs/llm_rule_interpretation_prompt_v1.json")
    ap.add_argument("--llm-fallback-output", default="outputs/llm_rule_interpretation_fallback_v1.json")
    ap.add_argument("--report-output", default="outputs/agentscope_pipeline_report_v1.json")
    args = ap.parse_args()
    report = run_pipeline(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
