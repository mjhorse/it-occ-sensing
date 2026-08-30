#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PID_FILE="runtime/demo_server.pid"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  kill "$(cat "$PID_FILE")"
  echo "Stopped pid=$(cat "$PID_FILE")"
  rm -f "$PID_FILE"
else
  lsof -ti tcp:8780 | xargs -r kill || true
  rm -f "$PID_FILE"
  echo "No recorded running demo server; cleared port 8780 if needed."
fi
