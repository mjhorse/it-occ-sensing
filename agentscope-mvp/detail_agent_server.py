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
from typing import Any, Dict

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
])
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def main():
    server = Server(Config(app, host="127.0.0.1", port=8776, log_level="warning"))
    server.run()

if __name__ == "__main__":
    main()
