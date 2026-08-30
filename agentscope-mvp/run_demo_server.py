#!/usr/bin/env python3
"""Unified demo server: serves UI prototype and Agent streaming API from one origin."""
import mimetypes
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from uvicorn import Config, Server

from detail_agent_server import health, stream_detail_analysis, copilot_chat

BASE = Path(__file__).parent
UI_DIR = BASE.parent / "mvp" / "ui-prototype-v4-1"

NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"}

async def index(request):
    return FileResponse(UI_DIR / "index.html", headers=NO_CACHE_HEADERS)

async def rules(request):
    return FileResponse(UI_DIR / "rules.html", headers=NO_CACHE_HEADERS)

routes = [
    Route("/", index, methods=["GET"]),
    Route("/index.html", index, methods=["GET"]),
    Route("/rules.html", rules, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
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
