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
    compact = {
        "correlation_summary": result.get("correlation_summary"),
        "same_time_baseline_opinion": (result.get("same_time_baseline") or {}).get("opinion"),
        "topology_view": (result.get("topology_dependency_analysis") or {}).get("topology_view"),
        "topology_opinion": (result.get("topology_dependency_analysis") or {}).get("topology_opinion"),
        "suspected_inbound_callers": (result.get("topology_dependency_analysis") or {}).get("suspected_inbound_callers", [])[:6],
        "downstream_root_cause_clues": (result.get("topology_dependency_analysis") or {}).get("downstream_root_cause_clues", [])[:3],
        "matched_experience_rules": result.get("matched_experience_rules", []),
        "missing_inputs": result.get("missing_inputs", []),
        "suggested_next_actions": result.get("suggested_next_actions", []),
    }
    return (
        "你是IT OCC感知判读Agent。请基于以下结构化证据，用中文流式生成面向运维/业务用户的详情解读。\n"
        "输出必须分段、有逻辑层次，不要写成一整段。请严格按以下小标题输出：\n"
        "【结论摘要】1-2句说明是否关联、置信度和首要影响。\n"
        "【关键证据】分点说明同时间段基线、离散event、经验规则命中。\n"
        "【拓扑影响】必须以“被调用关系”为主语义：调用方 -> 中心预警 appid；说明哪些上游调用方/业务入口可能受到中心 appid 异常影响。不要把图解释成中心 appid 主动调用别人。\n"
        "【缺失输入】列出仍需补齐的数据；没有则写暂无。\n"
        "【建议动作】给出可执行的下一步核查/处置动作。\n"
        "约束：不要编造不存在的事实；每段最多3条；保持结构化表达。\n\n"
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
