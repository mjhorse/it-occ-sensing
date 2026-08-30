#!/usr/bin/env python3
"""Unified demo server: serves UI prototype and Agent streaming API from one origin."""
import mimetypes
import asyncio
import os
import json
import time
import shutil
from pathlib import Path


def load_local_env():
    """Load optional local secrets without printing them.

    This keeps the demo working even when it is restarted directly with
    `python run_demo_server.py` instead of the shell script that sources
    `.env.local`.
    """
    env_file = Path(__file__).parent / ".env.local"
    if not env_file.exists():
        return
    for raw in env_file.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()

from starlette.applications import Starlette
from starlette.responses import FileResponse, Response, JSONResponse
from starlette.routing import Mount, Route
from contextlib import asynccontextmanager
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from uvicorn import Config, Server

from detail_agent_server import health, stream_detail_analysis, copilot_chat
from simulation_agents import RUNTIME, agent_loop, runtime_status, tick_once, load_state, atomic_write_state, apply_user_directive

BASE = Path(__file__).parent
UI_DIR = BASE.parent / "mvp" / "ui-prototype-v4-1"
SIM_STATE_FILE = BASE / "runtime" / "simulation-state-v4.json"
SIM_STATE_BACKUP_DIR = BASE / "runtime" / "simulation-state-history"
SUPPORTED_SIM_SCHEMA_PREFIX = "it_occ_sensing_server_simulation_state.v"
CURRENT_SIM_SCHEMA_VERSION = "it_occ_sensing_server_simulation_state.v1"

NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"}

async def index(request):
    return FileResponse(UI_DIR / "index.html", headers=NO_CACHE_HEADERS)

async def rules(request):
    return FileResponse(UI_DIR / "rules.html", headers=NO_CACHE_HEADERS)


def _validate_simulation_state(state: dict) -> tuple[bool, str]:
    if not isinstance(state, dict):
        return False, "state must be object"
    schema = str(state.get("schema_version") or "")
    if not schema.startswith(SUPPORTED_SIM_SCHEMA_PREFIX):
        return False, f"unsupported schema_version {schema!r}; use backend migration, never frontend regeneration"
    payload = state.get("payload")
    if not isinstance(payload, dict):
        return False, "payload must be object"
    if not isinstance(payload.get("appids"), list) or not isinstance(payload.get("data"), dict):
        return False, "payload must contain appids[] and data{}"
    for appid in payload.get("appids"):
        item = payload.get("data", {}).get(appid)
        if not isinstance(item, dict):
            return False, f"missing data for appid {appid}"
        app = item.get("app") or {}
        if app.get("appid") and app.get("appid") != appid:
            return False, f"appid mismatch for {appid}"
    return True, "ok"


def _backup_existing_state(reason: str = "before-write") -> None:
    if not SIM_STATE_FILE.exists():
        return
    SIM_STATE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    target = SIM_STATE_BACKUP_DIR / f"simulation-state-v4.{ts}.{reason}.json"
    shutil.copy2(SIM_STATE_FILE, target)

async def simulation_state(request):
    SIM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if request.method == "GET":
        if not SIM_STATE_FILE.exists():
            return JSONResponse({"ok": False, "reason": "simulation state not initialized"}, status_code=404, headers=NO_CACHE_HEADERS)
        try:
            return JSONResponse(json.loads(SIM_STATE_FILE.read_text(errors="ignore")), headers=NO_CACHE_HEADERS)
        except Exception as exc:
            return JSONResponse({"ok": False, "reason": f"failed to read simulation state: {exc}"}, status_code=500, headers=NO_CACHE_HEADERS)
    if request.method == "DELETE":
        return JSONResponse({
            "ok": False,
            "reason": "frontend/API deletion is disabled: generated history is immutable; use an explicit backend versioned migration instead",
        }, status_code=405, headers=NO_CACHE_HEADERS)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "reason": "invalid simulation state"}, status_code=400, headers=NO_CACHE_HEADERS)
        ok, reason = _validate_simulation_state(payload)
        if not ok:
            return JSONResponse({"ok": False, "reason": reason}, status_code=400, headers=NO_CACHE_HEADERS)
        _backup_existing_state("before-api-put")
        payload.setdefault("schema_version", CURRENT_SIM_SCHEMA_VERSION)
        payload["server_saved_at"] = int(time.time() * 1000)
        payload["immutability_policy"] = "append-or-version-migrate-only; do not rewrite generated historical facts from the frontend"
        tmp = SIM_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        tmp.replace(SIM_STATE_FILE)
        return JSONResponse({"ok": True, "savedAt": payload["server_saved_at"], "schema_version": payload.get("schema_version")}, headers=NO_CACHE_HEADERS)
    except Exception as exc:
        return JSONResponse({"ok": False, "reason": f"failed to save simulation state: {exc}"}, status_code=500, headers=NO_CACHE_HEADERS)


async def simulation_agent_status(request):
    return JSONResponse(runtime_status(), headers=NO_CACHE_HEADERS)


async def simulation_agent_control(request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = str(payload.get("action") or "status")
    if action == "start":
        RUNTIME.running = True
        RUNTIME.tick_sec = max(0.5, float(payload.get("tick_sec") or RUNTIME.tick_sec or 1))
        RUNTIME.speed = max(1, int(payload.get("speed") or RUNTIME.speed or 1))
        RUNTIME.last_summary = "SimulationDataAgent / WarningCalculationAgent 已启动"
    elif action == "pause":
        RUNTIME.running = False
        RUNTIME.last_summary = "SimulationDataAgent / WarningCalculationAgent 已暂停"
    elif action == "tick":
        async with RUNTIME.lock:
            state = load_state()
            result = tick_once(state, int(payload.get("seconds") or RUNTIME.speed or 1))
            atomic_write_state(state)
        return JSONResponse({"ok": True, "result": result, "status": runtime_status()}, headers=NO_CACHE_HEADERS)
    else:
        return JSONResponse({"ok": action == "status", "status": runtime_status()}, headers=NO_CACHE_HEADERS)
    return JSONResponse({"ok": True, "status": runtime_status()}, headers=NO_CACHE_HEADERS)


async def simulation_agent_chat(request):
    try:
        payload = await request.json()
        message = str(payload.get("message") or "").strip()
        if not message:
            return JSONResponse({"ok": False, "reason": "message required"}, status_code=400, headers=NO_CACHE_HEADERS)
        result = await apply_user_directive(message, payload.get("appid"))
        if not RUNTIME.running:
            RUNTIME.running = True
        return JSONResponse({**result, "status": runtime_status()}, headers=NO_CACHE_HEADERS)
    except Exception as exc:
        return JSONResponse({"ok": False, "reason": f"simulation agent chat failed: {type(exc).__name__}: {exc}"}, status_code=500, headers=NO_CACHE_HEADERS)


async def topology_agent_status(request):
    return JSONResponse({
        "ok": True,
        "agent": "TopologyManagementAgent",
        "policy": "UModel is the topology source of truth; topology edits must update UModel, not browser-local fallback.",
        "umodel_addr": os.environ.get("UMODEL_ADDR", "http://localhost:18080"),
        "umodel_workspace": os.environ.get("UMODEL_WORKSPACE", "itocc-demo"),
    }, headers=NO_CACHE_HEADERS)


routes = [
    Route("/", index, methods=["GET"]),
    Route("/index.html", index, methods=["GET"]),
    Route("/rules.html", rules, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/api/simulation/state", simulation_state, methods=["GET", "PUT", "DELETE"]),
    Route("/api/simulation-agent/status", simulation_agent_status, methods=["GET"]),
    Route("/api/simulation-agent/control", simulation_agent_control, methods=["POST"]),
    Route("/api/simulation-agent/chat", simulation_agent_chat, methods=["POST"]),
    Route("/api/topology-agent/status", topology_agent_status, methods=["GET"]),
    Route("/agent/detail-analysis/stream", stream_detail_analysis, methods=["POST"]),
    Route("/agent/copilot/chat", copilot_chat, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory=UI_DIR), name="static"),
]

@asynccontextmanager
async def lifespan(app):
    # Backend agents own runtime writes. Start them automatically so the
    # persisted simulation state keeps moving even when the browser is read-only.
    RUNTIME.running = os.environ.get("SIMULATION_AGENTS_AUTOSTART", "1") != "0"
    if RUNTIME.running:
        RUNTIME.last_summary = "SimulationDataAgent / WarningCalculationAgent 已自动启动"
    task = asyncio.create_task(agent_loop())
    try:
        yield
    finally:
        task.cancel()


app = Starlette(routes=routes, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def main():
    print(f"Unified demo server: http://127.0.0.1:8780/index.html")
    print(f"UI dir: {UI_DIR}")
    Server(Config(app, host="127.0.0.1", port=8780, log_level="warning")).run()

if __name__ == "__main__":
    main()
