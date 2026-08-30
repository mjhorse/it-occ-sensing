#!/usr/bin/env bash
# Import the IT OCC demo topology model/data into a running UModel server.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/../../../../.." && pwd)"
UMODEL_REPO="${UMODEL_REPO:-/Users/mjhorse/unifiedmodel}"
UMODEL_ADDR="${UMODEL_ADDR:-http://localhost:18080}"
UMODEL_WORKSPACE="${UMODEL_WORKSPACE:-itocc-demo}"

MODEL_DIR="$SCRIPT_DIR/model"
DATA_DIR="$SCRIPT_DIR/sample-data"

if [[ ! -d "$UMODEL_REPO" ]]; then
  echo "UModel repo not found: $UMODEL_REPO" >&2
  exit 1
fi

python3 "$SCRIPT_DIR/convert_sqlite_graph_to_umodel.py" --out "$DATA_DIR" >/tmp/itocc-umodel-convert.json

cd "$UMODEL_REPO"
if ! go run ./cmd/umctl -addr "$UMODEL_ADDR" workspace get "$UMODEL_WORKSPACE" >/tmp/itocc-umodel-workspace.json 2>/tmp/itocc-umodel-workspace.err; then
  go run ./cmd/umctl -addr "$UMODEL_ADDR" workspace create "$UMODEL_WORKSPACE" \
    "{\"name\":\"IT OCC UModel Demo\",\"description\":\"IT OCC sensing topology demo workspace\"}" \
    >/tmp/itocc-umodel-workspace.json
fi

go run ./cmd/umctl -addr "$UMODEL_ADDR" umodel import "$UMODEL_WORKSPACE" "$MODEL_DIR" >/tmp/itocc-umodel-import.json
go run ./cmd/umctl -addr "$UMODEL_ADDR" entity write "$UMODEL_WORKSPACE" "$DATA_DIR/entities.json" >/tmp/itocc-umodel-entities.json
go run ./cmd/umctl -addr "$UMODEL_ADDR" topo write "$UMODEL_WORKSPACE" "$DATA_DIR/relations.json" >/tmp/itocc-umodel-relations.json

cat <<EOF
IT OCC UModel demo imported.
- addr: $UMODEL_ADDR
- workspace: $UMODEL_WORKSPACE
- model: $MODEL_DIR
- data: $DATA_DIR
- conversion: /tmp/itocc-umodel-convert.json
- import result: /tmp/itocc-umodel-import.json
- entity write result: /tmp/itocc-umodel-entities.json
- relation write result: /tmp/itocc-umodel-relations.json

Use with AgentScope MVP:
TOPOLOGY_PROVIDER=umodel UMODEL_ADDR=$UMODEL_ADDR UMODEL_WORKSPACE=$UMODEL_WORKSPACE python3 graph_analysis.py com.sale.quote.center
EOF
