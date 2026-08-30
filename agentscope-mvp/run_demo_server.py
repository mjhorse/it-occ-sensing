#!/usr/bin/env python3
"""Unified demo server: serves UI prototype and Agent streaming API from one origin."""
import mimetypes
import os
import json
import time
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
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from uvicorn import Config, Server

from detail_agent_server import health, stream_detail_analysis, copilot_chat

BASE = Path(__file__).parent
UI_DIR = BASE.parent / "mvp" / "ui-prototype-v4-1"
SIM_STATE_FILE = BASE / "runtime" / "simulation-state-v4.json"

NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"}

async def index(request):
    return FileResponse(UI_DIR / "index.html", headers=NO_CACHE_HEADERS)

async def rules(request):
    return FileResponse(UI_DIR / "rules.html", headers=NO_CACHE_HEADERS)

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
        try:
            if SIM_STATE_FILE.exists():
                SIM_STATE_FILE.unlink()
            return JSONResponse({"ok": True, "deleted": True}, headers=NO_CACHE_HEADERS)
        except Exception as exc:
            return JSONResponse({"ok": False, "reason": f"failed to delete simulation state: {exc}"}, status_code=500, headers=NO_CACHE_HEADERS)
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("payload"), dict):
            return JSONResponse({"ok": False, "reason": "invalid simulation state"}, status_code=400, headers=NO_CACHE_HEADERS)
        payload["server_saved_at"] = int(time.time() * 1000)
        tmp = SIM_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        tmp.replace(SIM_STATE_FILE)
        return JSONResponse({"ok": True, "savedAt": payload["server_saved_at"]}, headers=NO_CACHE_HEADERS)
    except Exception as exc:
        return JSONResponse({"ok": False, "reason": f"failed to save simulation state: {exc}"}, status_code=500, headers=NO_CACHE_HEADERS)


routes = [
    Route("/", index, methods=["GET"]),
    Route("/index.html", index, methods=["GET"]),
    Route("/rules.html", rules, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/api/simulation/state", simulation_state, methods=["GET", "PUT", "DELETE"]),
    Route("/agent/detail-analysis/stream", stream_detail_analysis, methods=["POST"]),
    Route("/agent/copilot/chat", copilot_chat, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory=UI_DIR), name="static"),
]

app = Starlette(routes=routes)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def main():
    print(f"Unified demo server: http://127.0.0.1:8780/index.html")
    print(f"UI dir: {UI_DIR}")
    Server(Config(app, host="127.0.0.1", port=8780, log_level="warning")).run()

if __name__ == "__main__":
    main()
