#!/usr/bin/env python3
"""Streaming local agent server for IT OCC detail analysis.

Endpoint:
- GET /health
- POST /agent/detail-analysis/stream  (NDJSON chunks)
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from uvicorn import Config, Server
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.middleware.cors import CORSMiddleware
import httpx

from detail_analysis_pipeline import run_pipeline

try:
    from anthropic import AsyncAnthropic
except Exception:  # pragma: no cover - optional runtime dependency
    AsyncAnthropic = None

BASE = Path(__file__).parent
DEFAULT_REQUEST = BASE / "inputs" / "sample_detail_request_v1.json"


def _model_config() -> Dict[str, Any]:
    # Mirror Claude Code preference when available, redacting actual secrets.
    model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("CLAUDE_MODEL") or "claude-opus-4-1-20250805"
    return {
        "provider": "anthropic",
        "model_alias_from_claude_code": "opusplan",
        "effective_model": model,
        "effective_mode": "anthropic_stream_if_key_available_else_local_fallback",
        "has_anthropic_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "has_anthropic_base_url": bool(os.environ.get("ANTHROPIC_BASE_URL")),
    }


async def health(request: Request):
    return JSONResponse({
        "ok": True,
        "model_config": _model_config(),
        "topology_config": {
            "provider": os.environ.get("TOPOLOGY_PROVIDER", "sqlite"),
            "umodel_addr": os.environ.get("UMODEL_ADDR", "http://localhost:8080"),
            "umodel_workspace": os.environ.get("UMODEL_WORKSPACE", "itocc-demo"),
        },
    })


def _write_tmp_request(payload: Dict[str, Any]) -> Path:
    if not payload:
        return DEFAULT_REQUEST
    tmp = Path(tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", dir=BASE / "inputs").name)
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return tmp


class Args:
    def __init__(self, request: str, output: str, report: str):
        self.request = request
        self.output = output
        self.report = report


def _llm_prompt(result: Dict[str, Any]) -> str:
    topology = result.get("topology_dependency_analysis") or {}
    compact = {
        "correlation_summary": result.get("correlation_summary"),
        "timeline_reasoning": result.get("timeline_reasoning", [])[:8],
        "same_time_baseline_opinion": (result.get("same_time_baseline") or {}).get("opinion"),
        "topology_view": topology.get("topology_view"),
        "topology_opinion": topology.get("topology_opinion"),
        "suspected_inbound_callers": topology.get("suspected_inbound_callers", [])[:6],
        "downstream_root_cause_clues": topology.get("downstream_root_cause_clues", [])[:3],
        "matched_experience_rules": result.get("matched_experience_rules", []),
        "missing_inputs": result.get("missing_inputs", []),
        "suggested_next_actions": result.get("suggested_next_actions", []),
    }
    return (
        "你是面向一线运维/SRE的IT OCC预警研判助手。请不要讲系统内部如何推理、不要说‘我作为Agent’。"
        "你的目标是把排查结果、证据依据和下一步动作讲清楚，并且必须和页面上方的‘时间轴 × 拓扑 × 假设验证DAG’一一对应。\n"
        "输出必须用中文，按以下小标题：\n"
        "【一、当前该怎么判断】用1-2句给运维结论：是否应继续作为同一异常链路核对、当前置信度/影响面。\n"
        "【二、沿时间轴看证据】按 T-10min / T / T+10min 说明：先发生什么、当前预警是什么、之后有哪些用户影响或事件承接；只引用输入里存在的事实。\n"
        "【三、沿拓扑看影响】必须以‘调用方 -> 中心预警 appid’为主语义，说明哪些上游调用方/业务入口需要优先核对；不要把图解释成中心 appid 主动调用别人。\n"
        "【四、假设与验证结论】用鱼骨图/有向无环图节点的语言表达：假设、证据、验证结论、缺失输入。每条都要能对应页面上方节点。\n"
        "【五、下一步排查动作】按运维实际操作顺序列出3-5步：先查什么、再查什么、满足什么条件升级/关闭。\n"
        "约束：不要编造不存在的事实；不要复述系统流程日志；不要讲模型/Agent内部机制；每段最多5条，表达面向值班人员。\n\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
    )


async def stream_llm_refinement(result: Dict[str, Any]):
    cfg = _model_config()
    if not cfg["has_anthropic_api_key"]:
        yield {"type": "llm_status", "agent": "LLMRefineAgent", "message": "未检测到 ANTHROPIC_API_KEY，跳过真实大模型流式 refine，保留结构化 Agent 输出。"}
        return
    try:
        endpoint = "自定义 Anthropic-compatible endpoint" if cfg["has_anthropic_base_url"] else "Anthropic 默认 endpoint"
        yield {"type": "llm_start", "agent": "LLMRefineAgent", "message": f"通过{endpoint}连接模型 {cfg['effective_model']} 进行流式解读。"}
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        payload = {
            "model": cfg["effective_model"],
            "max_tokens": 900,
            "temperature": 0.2,
            "stream": True,
            "messages": [{"role": "user", "content": _llm_prompt(result)}],
        }
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", f"{base_url}/v1/messages", json=payload, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        text = delta.get("text")
                        if text:
                            yield {"type": "llm_delta", "agent": "LLMRefineAgent", "text": text}
        yield {"type": "llm_done", "agent": "LLMRefineAgent", "message": "大模型流式解读完成。"}
    except Exception as exc:
        yield {"type": "llm_error", "agent": "LLMRefineAgent", "message": f"大模型流式 refine 未完成，已回退结构化输出：{type(exc).__name__}: {exc}"}


def _compact_copilot_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the chat prompt bounded while preserving operational evidence."""
    ctx = payload.get("context") or payload
    focus = ctx.get("focus_event") or {}
    topology = ctx.get("topology_context") or {}
    return {
        "schema_version": ctx.get("schema_version"),
        "appid": ctx.get("appid"),
        "app_name": ctx.get("app_name"),
        "focus_event": focus,
        "metrics_window": ctx.get("metrics_window"),
        "metric_around_window": ctx.get("metric_around_window"),
        "discrete_events": (ctx.get("discrete_events") or [])[:20],
        "topology_context": {
            "focus_node": topology.get("focus_node"),
            "related_app_signals": (topology.get("related_app_signals") or [])[:12],
            "missing_inputs": topology.get("missing_inputs") or [],
        },
    }


def _copilot_system_prompt(context: Dict[str, Any]) -> str:
    return (
        "你是 IT OCC Copilot，一个面向一线运维/SRE的持续对话式预警研判助手。"
        "你正在围绕同一个 focus event 与用户多轮协作排查。\n"
        "行为要求：\n"
        "1. 不要讲模型/Agent/系统内部流程；直接回答运维问题。\n"
        "2. 每次回答都要尽量引用当前上下文里的时间轴、拓扑、指标、离散事件作为依据。\n"
        "3. 拓扑语义必须保持：调用方 -> 中心预警 appid；不要反向解释。\n"
        "4. 如果用户问下一步，给可执行排查顺序；如果问原因，区分已证实、候选假设、缺失证据。\n"
        "5. 不要编造不存在的指标名、阈值、工单字段；缺失就明确说缺失。\n"
        "6. 如果用户问访问量/请求量/流量/指标在事件前后几分钟的变化，必须优先读取 metric_around_window 中对应指标的 before/after/change，不要返回通用事件摘要。\n"
        "7. 回答要短而实用，默认 3-6 条；必要时用编号。\n\n"
        "当前预警上下文 JSON：\n" + json.dumps(context, ensure_ascii=False, indent=2)
    )


def _format_metric_change(metric_name: str, metric: Dict[str, Any], label: str) -> str:
    before = metric.get("before") or {}
    after = metric.get("after") or {}
    change = metric.get("change") or {}
    if before.get("avg") is None or after.get("avg") is None:
        return f"{label}：事件前后 5 分钟数据不足，无法计算变化。"
    pct = change.get("delta_pct")
    pct_text = f"（{pct:+.1f}%）" if isinstance(pct, (int, float)) else ""
    unit = "次/采样点" if metric_name == "request_count" else ""
    return (
        f"{label}：事件前 5 分钟均值 {before.get('avg')}{unit}，事件后 5 分钟均值 {after.get('avg')}{unit}，"
        f"{change.get('direction', '变化')} {change.get('delta')}{unit}{pct_text}。"
        f"前窗口范围 {before.get('min')}~{before.get('max')}，后窗口范围 {after.get('min')}~{after.get('max')}，样本数 {before.get('count')}/{after.get('count')}。"
    )


def _metric_question_reply(question: str, context: Dict[str, Any]) -> str | None:
    q = question or ""
    around = context.get("metric_around_window") or {}
    metrics = around.get("metrics") or {}
    if any(k in q for k in ["访问量", "请求量", "流量", "request", "请求数"]):
        metric = metrics.get("request_count")
        if not metric:
            return "当前上下文没有 request_count 的前后窗口序列，所以不能回答访问量变化；需要前端把事件前后指标点传给 Copilot。"
        return "\n".join([
            _format_metric_change("request_count", metric, "用户访问量/请求量"),
            "解读：如果后 5 分钟均值明显上升，说明事件后流量压力变大，需同时核对容量/限流；如果下降，则可能是用户受影响后放弃访问或入口被降级；如果持平，则更偏向服务处理能力或依赖异常。",
            "依据：使用当前 focus event 时间点切分 T-5min~T 与 T~T+5min 的 request_count 序列。",
        ])
    if any(k in q for k in ["时延", "延迟", "p95", "P95"]):
        metric = metrics.get("p95_latency_ms")
        if metric:
            return _format_metric_change("p95_latency_ms", metric, "P95 时延")
    if any(k in q for k in ["错误率", "失败率", "error"]):
        metric = metrics.get("error_rate")
        if metric:
            return _format_metric_change("error_rate", metric, "错误率")
    return None


def _fallback_copilot_reply(question: str, context: Dict[str, Any], history: List[Dict[str, str]]) -> str:
    app = context.get("app_name") or context.get("appid") or "当前服务"
    focus = context.get("focus_event") or {}
    events = context.get("discrete_events") or []
    metrics = context.get("metrics_window") or {}
    related = ((context.get("topology_context") or {}).get("related_app_signals") or [])[:5]
    missing = ((context.get("topology_context") or {}).get("missing_inputs") or [])
    q = (question or "").strip()
    metric_reply = _metric_question_reply(q, context)
    if metric_reply:
        return metric_reply
    event_lines = []
    for e in events[:6]:
        event_lines.append(f"- {e.get('event_time','')}｜{e.get('event_type','event')}｜{e.get('description') or e.get('title') or e.get('event_id')}")
    topo_lines = []
    for r in related:
        topo_lines.append(f"- {r.get('app_name') or r.get('appid')} -> {app}：同窗口事件 {len(r.get('events') or [])} 条，P95比例 {((r.get('metrics') or {}).get('p95_latency_ratio') or '缺失')}，错误率比例 {((r.get('metrics') or {}).get('error_rate_ratio') or '缺失')}")
    if any(k in q for k in ["下一步", "怎么", "排查", "处理", "动作"]):
        return "\n".join([
            f"建议先按这条链路排：{app} 当前预警不是单看一个点，而是要把时间轴、拓扑和用户侧反馈合起来。",
            "1. 先确认中心服务当前指标：P95/错误率/请求量是否仍高于同窗口基线。",
            "2. 再按拓扑核对调用方 -> 中心服务的入口体验，优先看同窗口也有异常信号的调用方。",
            "3. 回看 T-10 到 T+10 的离散事件：告警、SD、心声、变更、事件单是否能串成同一条链。",
            "4. 若事件单已承接，补齐关闭原因/处置进展；若缺少上游调用方实时指标，先不要扩大定责。",
            ("缺失输入：" + "；".join(missing)) if missing else "缺失输入：暂无明显缺失。",
        ])
    if any(k in q for k in ["拓扑", "上游", "调用", "影响"]):
        return "\n".join([f"拓扑上要按“调用方 -> {app}”理解，也就是这些入口/服务可能被中心预警服务的异常影响：", *(topo_lines or ["- 当前上下文没有强拓扑关联信号。"]), "结论：优先核对这些调用方的用户体验和错误率/时延，不要把它解释成中心服务主动调用它们。"])
    return "\n".join([
        f"围绕 {app} 的当前预警，我看到的核心事实是：{focus.get('description') or focus.get('title') or focus.get('event_id') or '当前 focus event'}。",
        "时间轴证据：", *(event_lines or ["- 当前上下文没有离散事件明细。"]),
        "指标窗口：" + json.dumps(metrics, ensure_ascii=False),
        "可继续问我：‘下一步怎么排查’、‘拓扑上先看谁’、‘哪些证据支持/反证这个判断’。",
    ])


async def copilot_chat(request: Request):
    payload = await request.json() if request.headers.get("content-length") not in [None, "0"] else {}
    question = (payload.get("message") or "").strip()
    history = payload.get("history") or []
    context = _compact_copilot_context(payload)
    if not question:
        return JSONResponse({"ok": False, "error": "message is required"}, status_code=400)
    cfg = _model_config()
    if not cfg["has_anthropic_api_key"]:
        return JSONResponse({
            "ok": True,
            "mode": "local_fallback",
            "agent": "IT_OCC_CopilotAgent",
            "message": _fallback_copilot_reply(question, context, history),
            "model_config": cfg,
        })
    try:
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        messages = []
        for h in history[-12:]:
            role = h.get("role") if h.get("role") in ["user", "assistant"] else "user"
            content = str(h.get("content") or "")[:2000]
            if content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})
        api_payload = {
            "model": cfg["effective_model"],
            "max_tokens": 900,
            "temperature": 0.2,
            "system": _copilot_system_prompt(context),
            "messages": messages,
        }
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{base_url}/v1/messages", json=api_payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        return JSONResponse({"ok": True, "mode": "anthropic", "agent": "IT_OCC_CopilotAgent", "message": text, "model_config": cfg})
    except Exception as exc:
        return JSONResponse({
            "ok": True,
            "mode": "error_fallback",
            "agent": "IT_OCC_CopilotAgent",
            "message": _fallback_copilot_reply(question, context, history),
            "warning": f"LLM call failed, used fallback: {type(exc).__name__}: {exc}",
            "model_config": cfg,
        })


async def stream_detail_analysis(request: Request):
    payload = await request.json() if request.headers.get("content-length") not in [None, "0"] else {}

    async def gen():
        yield json.dumps({"type": "agent_start", "agent": "DetailAnalysisOrchestrator", "message": "开始连接 AgentScope 详情解读链路。", "model_config": _model_config()}, ensure_ascii=False) + "\n"
        await asyncio.sleep(0.12)
        yield json.dumps({"type": "agent_delta", "agent": "ContextCollectorAgent", "message": "收集 focus event、最近窗口离散event，并查询最近几天同时间段基线。"}, ensure_ascii=False) + "\n"
        await asyncio.sleep(0.12)
        topo_provider = os.environ.get("TOPOLOGY_PROVIDER", "sqlite").strip().lower()
        topo_msg = "连接 UModel .topo 拓扑服务，查询调用方 → 中心预警 appid 与下游依赖。" if topo_provider == "umodel" else "连接本地 SQLite 图数据库，查询 appid 依赖路径和疑似传播链。"
        yield json.dumps({"type": "agent_delta", "agent": "TopologyDependencyAgent", "message": topo_msg}, ensure_ascii=False) + "\n"
        await asyncio.sleep(0.12)
        req_path = _write_tmp_request(payload)
        out_name = "outputs/stream_agent_detail_analysis_latest.json"
        report_name = "outputs/stream_detail_analysis_report_latest.json"
        report = run_pipeline(Args(str(req_path.relative_to(BASE)), out_name, report_name))
        for stage in report["stages"]:
            yield json.dumps({"type": "agent_stage", **stage}, ensure_ascii=False) + "\n"
            await asyncio.sleep(0.08)
        result = json.loads((BASE / out_name).read_text())
        # If API key exists, this is the insertion point for a real Anthropic streaming refinement.
        # MVP keeps deterministic structure while preserving streaming UX.
        yield json.dumps({"type": "structured_result", "data": result}, ensure_ascii=False) + "\n"
        await asyncio.sleep(0.05)
        async for llm_msg in stream_llm_refinement(result):
            yield json.dumps(llm_msg, ensure_ascii=False) + "\n"
            await asyncio.sleep(0.01)
        yield json.dumps({"type": "agent_done", "message": "Agent 解读完成。"}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


app = Starlette(routes=[
    Route("/health", health, methods=["GET"]),
    Route("/agent/detail-analysis/stream", stream_detail_analysis, methods=["POST"]),
    Route("/agent/copilot/chat", copilot_chat, methods=["POST"]),
])
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def main():
    server = Server(Config(app, host="127.0.0.1", port=8776, log_level="warning"))
    server.run()

if __name__ == "__main__":
    main()
