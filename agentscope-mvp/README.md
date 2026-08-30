# AgentScope MVP｜经验规则命中与反馈闭环

本目录用于验证 IT OCC 感知视算 Agent 的最小闭环：

```text
candidate_v2 → mock Rule Evaluator → judgement_result_v2 → human feedback → experience rule stats update
```

## 文件结构

- `inputs/sample_candidate_v2.json`：包含当前快照、历史窗口、检索上下文的候选预警。
- `kb/experience-rules.jsonl`：条目化经验规则注册表。
- `rule_evaluator.py`：mock AgentScope Rule Evaluator，输出命中/未命中规则、原因和置信度影响。
- `agentscope_pipeline.py`：AgentScope-style 编排入口；当前环境未安装 `agentscope` 时使用本地 agent fallback，保持 CandidateReader / RuleEvaluator / EvidenceNarrator / FeedbackLearning 四段边界。
- `outputs/sample_evidence_narrative_v1.json`：面向用户的证据叙述输出，强调 event 是离散信息点总称，事件单只是其中一种。
- `outputs/sample_judgement_v2.json`：样例 Agent 判读结果。
- `feedback/sample_feedback_true_positive.json`：人工复盘反馈样例。
- `apply_feedback.py`：将人工反馈回写为经验规则命中统计。
- `kb/experience-rules.updated.jsonl`：反馈统计更新后的规则注册表示例。

## 运行

```bash
cd artifacts/it-occ-sensing-agent/agentscope-mvp
./rule_evaluator.py
./apply_feedback.py
./agentscope_pipeline.py
```

## 当前验证点

- Agent 输出命中的经验规则：`EXP-CHANGE-LATENCY-001`。
- Agent 输出未命中的相关规则：`EXP-LOW-TRAFFIC-SPIKE-001`、`EXP-MAINTENANCE-WINDOW-001`。
- 输出包含 `why_matched` / `why_not_matched`。
- 人工反馈可更新 `hit_count`、`confirmed_correct`、`confirmed_wrong`、`precision`。


## AgentScope-style 编排说明

当前机器尚未安装 Python `agentscope` 包，所以 MVP 先采用可运行的本地 fallback：

1. `CandidateReaderAgent` 读取候选预警与离散 event。
2. `RuleEvaluatorAgent` 调用确定性规则 evaluator，输出判读结果。
3. `EvidenceNarratorAgent` 将命中证据整理成用户可读说明，避免暴露数据库/开发者字段。
4. `FeedbackLearningAgent` 在提供人工反馈时更新经验规则统计。

这样先把 AgentScope 的角色边界、输入输出 artifact 和闭环跑通；后续安装真实 `agentscope` 后，可以把这四段迁移成正式 AgentScope Agent，而不改变样例数据和验证口径。

## 规则经验 + LLM 解读结合

RuleEvaluatorAgent 不应只等于 Python if/else。MVP 当前拆成两层：

- 确定性经验规则层：负责可审计、可复盘、可统计的命中/未命中判断。
- LLM 语义解读层：负责处理用户原话、SD 描述、心声、事件单描述等泛化非结构化 event，识别同义表达、用户症状、影响范围、反证和缺失输入。

`llm_interpretation.py` 先生成标准 LLM prompt 与本地 fallback 输出；真实模型接入后，`LLMInterpretationAgent` 应用 LLM 输出去补充 `EvidenceNarratorAgent` 的用户可读解释，而不是替代经验规则审计。
