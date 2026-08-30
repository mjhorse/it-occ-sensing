#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# UModel/OModel is the single topology source. If UModel is unavailable,
# the analysis pipeline must fail fast instead of falling back to SQLite or
# browser-generated topology.
export TOPOLOGY_PROVIDER="${TOPOLOGY_PROVIDER:-umodel}"
export UMODEL_ADDR="${UMODEL_ADDR:-http://localhost:18080}"
export UMODEL_WORKSPACE="${UMODEL_WORKSPACE:-itocc-demo}"

exec .venv/bin/python run_demo_server.py
