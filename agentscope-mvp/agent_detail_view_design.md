# Agent 详情解读界面设计 v1

## 目标

当用户在时间轴中点击某个时间点或离散 event 并展开详情时，页面不只展示该单点字段，而是展示 Agent 对最近一段时间关联性的解读：

- 这个 event 是否与附近告警、SD、心声、变更、事件单有关？
- 这些信号是否构成同一条异常链路？
- 命中了哪些经验规则？
- 还缺什么输入，下一步该看哪里？

## 2026-08-21 用户反馈修正

1. 详情标题不能写成“信号详情｜事件单”这类固定标题，因为 `event` 是离散信息点总称，事件单只是其中一种。标题应把当前 focus event 的核心上下文整合表达：`appid + 时间 + event类型 + 关键标题/摘要`。
2. 首页点击附近事件与 Agent 分析窗口口径必须统一：统一按 focus event 时间点的 **前后各 10 分钟（±10min）** 做关联评估。不要首页显示“8+附近”，分析页却使用不同窗口或模糊口径。
3. 时间轴横轴单位、纵轴单位与刻度不能视觉重叠；单位应从刻度区域移出，使用独立 axis label / lane header / corner label，并为刻度文本预留足够 padding。

---

## 页面结构

详情弹窗建议分三层：

1. **当前信号详情**
   - 标题格式：`{appid}｜{HH:mm:ss}｜{event_type_display}｜{event_title}`。
   - 示例：`com.iam.account.auth｜14:08:32｜告警｜登录接口 P95 延迟升高`。
   - 示例：`com.sale.quote.center｜14:11:05｜SD 单｜用户反馈报价提交失败`。
   - 禁止固定写成“信号详情｜事件单”；除非 focus event 的类型确实是事件单，也仍应显示 appid、时间和标题。
   - 信号类型、时间、级别/状态、摘要、业务描述。
   - 只放必要事实，不放“这是指标证据”这类背景说明。

2. **Agent 关联解读**
   - 必须保留 Agent 解读的交互过程，不允许只展示静态结论或只展示拓扑图。
   - 交互过程按步骤展示：收集上下文 → 结构化证据 → 规则/经验匹配 → 拓扑影响推理 → 综合研判输出。
   - 每一步显示“正在分析/已完成/缺失输入/无法判断”的状态，便于用户理解 Agent 是如何得出结论的。
   - 关联强度：强 / 中 / 弱 / 暂未关联。
   - 一句话结论：以 focus event 为中心，前后各 10 分钟内哪些离散 event 形成关联。
   - 时间线：统一按 `T-10min → T → T+10min` 展示附近 event 及其解释。
   - 命中经验规则：展示规则名和命中原因。
   - 缺失输入：例如拓扑实时状态、事件单关闭原因、变更影响面。

3. **结构化推理结果**
   - 预警分析弹出框中必须按逻辑分段显示 Agent 推理结果，而不是一整段自然语言。
   - 推荐分段：
     1. 结论摘要：置信度、影响等级、是否建议处置；
     2. 分析窗口与输入：focus event、appid、`T-10min ~ T+10min`、输入源；
     3. 关键证据链：指标、告警、SD/IM、心声/论坛、变更、warning 的时间关系；
     4. 规则/经验判断：命中规则、未命中规则、反证和缺失条件；
     5. 拓扑依赖推理：一层关联/全景端到端路径、疑似源头、受影响节点；
     6. 缺失输入与反证：明确哪些信息不足，哪些现象降低置信度；
     7. 建议动作：观察、通知 Owner、查看拓扑、拉起 Warroom dry-run 等。
   - 每段必须优先展示结构化条目；自然语言只作为解释补充。

4. **建议动作**
   - 查看拓扑传播链。
   - 确认对应事件单是否仍处理中。
   - 核对变更影响范围。
   - 拉起 Owner/SRE。

## AgentScope 分工

- `ContextCollectorAgent`：根据 focus event 拉取 `T-10min` 到 `T+10min` 的指标、离散event、拓扑、基线；除非用户手动切换窗口，否则首页附近事件、详情分析和 Agent 输入必须使用同一窗口。
- `RuleEvaluatorAgent`：执行经验规则匹配，输出可审计的 matched/unmatched。
- `LLMInterpretationAgent`：处理非结构化文本，识别用户症状、同义表达、影响范围、反证。
- `CorrelationNarratorAgent`：组合规则结果和 LLM 语义，生成用户可读的关联解读。
- `FeedbackLearningAgent`：接收用户/复盘反馈，回写规则统计和语义样例。

## MVP 接入方式

当前前端先用本地函数 `agentCorrelationAnalysis()` 生成静态模拟解读；下一步替换为：

```text
showEventDetail(event)
  → POST /agent/detail-analysis
  → AgentScope pipeline
  → 返回 agent_detail_view_contract_v1.json response
  → 渲染 Agent 解读交互过程 + 结构化推理结果 + 建议动作
```

## 输出契约

见：`outputs/agent_detail_view_contract_v1.json`

建议补充字段：

```json
{
  "focus_event_header": {
    "appid": "com.iam.account.auth",
    "focus_time": "2026-08-21T14:08:32+08:00",
    "event_type": "alert",
    "event_type_display": "告警",
    "title": "登录接口 P95 延迟升高",
    "display_title": "com.iam.account.auth｜14:08:32｜告警｜登录接口 P95 延迟升高"
  },
  "analysis_window": {
    "mode": "centered",
    "before_minutes": 10,
    "after_minutes": 10,
    "window_start": "2026-08-21T13:58:32+08:00",
    "window_end": "2026-08-21T14:18:32+08:00"
  }
}
```

预警分析弹出框建议输出契约：

```json
{
  "agent_interaction_steps": [
    {"step": 1, "name": "收集上下文", "status": "completed", "summary": "已读取 focus event、appid、±10分钟窗口、指标和离散 event"},
    {"step": 2, "name": "结构化证据", "status": "completed", "summary": "已归并告警、SD/IM、心声、变更、指标"},
    {"step": 3, "name": "规则/经验匹配", "status": "completed", "summary": "已检查触发规则、经验规则命中/未命中"},
    {"step": 4, "name": "拓扑影响推理", "status": "completed", "summary": "已分析一层关联和端到端路径"},
    {"step": 5, "name": "生成研判结论", "status": "completed", "summary": "已输出置信度、影响等级、缺失输入和建议动作"}
  ],
  "reasoning_sections": [
    {"section": "结论摘要", "items": []},
    {"section": "分析窗口与输入", "items": []},
    {"section": "关键证据链", "items": []},
    {"section": "规则/经验判断", "items": []},
    {"section": "拓扑依赖推理", "items": []},
    {"section": "缺失输入与反证", "items": []},
    {"section": "建议动作", "items": []}
  ]
}
```
