#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PID_FILE="runtime/demo_server.pid"
LOG_FILE="runtime/demo_server.log"
mkdir -p runtime
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Unified demo server already running: pid=$(cat "$PID_FILE")"
  echo "URL: http://127.0.0.1:8780/index.html"
  exit 0
fi
# Optional local secret env file. Keep this file out of git and never print it.
if [[ -f .env.local ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env.local
  set +a
fi
: "${ANTHROPIC_MODEL:=claude-sonnet-4-6}"
: "${TOPOLOGY_PROVIDER:=umodel}"
: "${UMODEL_ADDR:=http://localhost:18080}"
: "${UMODEL_WORKSPACE:=itocc-current}"
export ANTHROPIC_MODEL TOPOLOGY_PROVIDER UMODEL_ADDR UMODEL_WORKSPACE
nohup .venv/bin/python run_demo_server.py >"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
sleep 1
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Failed to start. See $LOG_FILE" >&2
  exit 1
fi
echo "Unified demo server started: pid=$(cat "$PID_FILE")"
echo "URL: http://127.0.0.1:8780/index.html"
echo "Health: http://127.0.0.1:8780/health"
echo "Log: $LOG_FILE"
