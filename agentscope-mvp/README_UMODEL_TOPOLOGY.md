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
- `graph_analysis.py`：强制 `TOPOLOGY_PROVIDER=umodel`；UModel 不可用、workspace 未导入、或 appid 不存在时 fail-fast，不允许回退 SQLite/dynamic fallback。

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

## 单一数据源与失败行为

拓扑数据的唯一事实源是 UModel 持久化 workspace。后续不允许：

- 前端按当前 `PAYLOAD` 即时生成拓扑边；
- 后端在 UModel 不可用时回退 SQLite；
- appid 不存在时按命名规则动态造 `*.web`、`*.mobile`、`*.cache` 等节点；
- 把同窗口指标/event 相关性伪装成调用拓扑。

如果 UModel 服务不可用、workspace 未导入、或 appid 不存在，`graph_analysis.py` 必须直接报错。页面和 Agent 只能显示“UModel 拓扑不可用/数据缺失”，不能生成第二套拓扑。

## 模拟数据持久化原则

IT OCC 模拟数据必须是一套可版本化、可审计、不可被前端刷新改写的持久化数据集。

要求：

- 生成发生在后端/构建步骤，输出带 `dataset_id`、`schema_version`、`generated_at`、`generator_version`。
- 前端只读取已生成 dataset，不在浏览器刷新时重算历史 series/events/topology。
- 已发布的历史 dataset 只追加新版本，不原地修改；如果模型或 schema 变化，用兼容读取或显式 migration 产出新 dataset 版本。
- 所有页面，包括时间轴、Agent 详情、Copilot、拓扑图，都基于同一个 dataset + UModel workspace 查询，只是建模视角不同。

## 2026-08-30 强化约束

- `/api/simulation/state` 是当前 UI 的模拟数据入口；GET 读取后端持久化快照，PUT 仅接受带兼容 `schema_version` 的完整后端状态，并在覆盖前写入 `runtime/simulation-state-history/` 备份；DELETE 已禁用。
- `mvp/ui-prototype-v4-1/index.html` 已改为只读后端快照：不再 `seedRecentData`、不再 `appendSimulatedData` 推进时间、不再通过 IndexedDB/file 模式兜底生成、不再用页面选择变化写回历史。
- “持续刷新”按钮改为“重新读取持久化数据”，语义是重新 GET 后端快照，不产生新点位或新事件。
- 历史数据不可由前端清理或按新规则重算；如旧数据与规则/模型不兼容，必须由后端 migration 生成新 `schema_version`/dataset，并保留旧版本备份。
