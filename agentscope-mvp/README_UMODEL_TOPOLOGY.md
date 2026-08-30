# IT OCC AgentScope MVP：UModel 拓扑接入说明

本目录已新增一条 UModel/OModel 拓扑接入路径，用来替代原本只读本地 SQLite mock graph 的方式。

## 已新增内容

- `umodel/model/`：IT OCC 最小 UModel model pack。
  - `itocc.app`：应用/服务/网关/DB/MQ/Cache 统一实体。
  - `itocc.app_*_itocc.app`：应用到应用的有向拓扑关系。
- `umodel/convert_sqlite_graph_to_umodel.py`：把当前 SQLite demo graph 转成 UModel runtime payload。
- `umodel/sample-data/entities.json`：由 `app_nodes` 生成的 UModel entities。
- `umodel/sample-data/relations.json`：由 `app_edges` 生成的 UModel relations。
- `umodel/sample-data/appid_entity_id_map.json`：稳定的 `appid -> __entity_id__` 映射。
- `umodel_topology_provider.py`：通过 UModel Query Service `.topo` 查询入向/出向关系。
- `graph_analysis.py`：新增 `TOPOLOGY_PROVIDER=umodel` 切换；UModel 不可用时自动回退 SQLite/dynamic fallback。

## 关系方向约定

统一使用真实调用方向：

```text
调用方 / 依赖方 src → 被调用方 / 被依赖方 dest
```

因此预警 appid 居中的主视图查询是入向关系：

```text
调用方 → 中心预警 appid
```

出向关系只作为根因排查线索：

```text
中心预警 appid → 下游依赖
```

## 启动和导入

先启动 UModel server，例如：

```bash
cd /Users/mjhorse/unifiedmodel
go run ./cmd/umodel-server --addr :18080 --graphstore memory
```

再导入 IT OCC demo model/data：

```bash
cd /Users/mjhorse/.openclaw/workspace/artifacts/it-occ-sensing-agent/agentscope-mvp
./umodel/setup_itocc_umodel_demo.sh
```

默认参数：

```text
UMODEL_ADDR=http://localhost:18080
UMODEL_WORKSPACE=itocc-demo
UMODEL_REPO=/Users/mjhorse/unifiedmodel
```

## 验证 provider

```bash
cd /Users/mjhorse/.openclaw/workspace/artifacts/it-occ-sensing-agent/agentscope-mvp
TOPOLOGY_PROVIDER=umodel \
UMODEL_ADDR=http://localhost:18080 \
UMODEL_WORKSPACE=itocc-demo \
python3 graph_analysis.py com.sale.quote.center
```

期望结果：

- `graph_store` 为 `umodel:http://localhost:18080/workspace/itocc-demo`。
- `inbound_paths` 包含：
  - `com.sale.order.portal → com.sale.quote.center`
  - `com.sale.mobile.app → com.sale.quote.center`
  - `com.partner.api.gateway → com.sale.quote.center`
  - `com.sale.contract.center → com.sale.quote.center`
- `downstream_paths` 包含中心 appid 到报价核心、合同中心、认证网关等依赖。

## 验证 Agent 详情流水线

```bash
cd /Users/mjhorse/.openclaw/workspace/artifacts/it-occ-sensing-agent/agentscope-mvp
TOPOLOGY_PROVIDER=umodel \
UMODEL_ADDR=http://localhost:18080 \
UMODEL_WORKSPACE=itocc-demo \
python3 detail_analysis_pipeline.py \
  --request inputs/sample_detail_request_v1.json \
  --output outputs/sample_agent_detail_analysis_umodel_v1.json \
  --report outputs/detail_analysis_pipeline_umodel_report_v1.json
```

期望报告中 `TopologyDependencyAgent` 显示：

```text
基于UModel拓扑识别 4 个关联 appid。
```

## 回退行为

如果设置了：

```text
TOPOLOGY_PROVIDER=umodel
```

但 UModel 服务不可用、workspace 未导入、或 appid 不存在，`graph_analysis.py` 会自动回退到原 SQLite/dynamic 逻辑，并在 graph 中写入：

```text
graph_store_fallback_reason
requested_graph_store=umodel
```

这样 demo 不会因为 UModel 未启动而直接失败。
