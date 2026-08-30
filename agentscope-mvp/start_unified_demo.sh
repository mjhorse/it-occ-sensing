#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Prefer UModel/OModel topology when a local UModel service is running.
# The analysis pipeline still falls back to SQLite/dynamic topology if UModel is unavailable.
export TOPOLOGY_PROVIDER="${TOPOLOGY_PROVIDER:-umodel}"
export UMODEL_ADDR="${UMODEL_ADDR:-http://localhost:18080}"
export UMODEL_WORKSPACE="${UMODEL_WORKSPACE:-itocc-demo}"

exec .venv/bin/python run_demo_server.py
