# Rollout failure policy: drop infra failures, retry transient ones

## Goal

Stop training on non-model rollout failures as reward 0: classify failures, drop agent/infra failures, retry transient fast failures once, surface counts

## 当前交付状态（2026-10-10 收尾核对）

实现及 AC1–AC4 已完成，任务保持 `in_progress` 等待 AC5 真实训练验收；AC5 需要单独批准费用，尚未执行。OfficeBench 镜像是否已包含修复仍待核实，本轮没有构建或部署。toolkit 合并事实见下文“Toolkit 交付”。

## 实施前现状（2026-10-10 读代码确认，历史背景）

- 训练是同步 on-policy：`trainer.v1.trainer_mode=agentcore_sync`（`backend/app/render/train.py:35`），单条 rollout 没有重试。
- toolkit `backends/verl/agent_loop.py:243-284`：
  - 超时、ACR 异常、`status_code != 200` 都记为 `error`。只要网关捕获到 token，就以 **reward=0 进入训练**（`_resolve_reward`），并写 `acr_failed=1`。
  - 一个 token 都没捕获到时抛 `RuntimeError`，不产生训练行。
- toolkit `app.py:184-204`：agent 代码里的任何异常都会存成 `{"status_code": 500, "stop_reason": str(e), "traceback": ...}`。这样一来，"模型把上下文用满了"和"环境代码有 bug"在 trainer 看来是同一类失败。
- verl 0.9.0 `trainer/ppo/v1/replay_buffer.py:103-112`、`agent_loop_tq.py:131-148`：agent loop 抛异常时，该 prompt 组标记为 `failure`。在 sync 模式下，默认行为是该组仍可采样、缺失的轨迹在下游补齐，同组其余样本照常训练。所以**从 agent loop 抛异常就等于只丢掉这一条**，不影响同组其余样本。
- 实测的反例：OfficeBench 的 `rl_app.py:98` 在 `logger.info(response.message['content'][0]['text'])` 处遇到空内容就抛 IndexError（评测 ev-916804751a #1，66 个 span 都正常）。这行在算 reward **之前**，所以 agent 可能已经完成了任务，结果却被当成 0 分惩罚。
- 已有的观测手段：`training/rollout_failure/total_missing_sessions`，以及 `acr_failed` 均值（Runs 页 health 图，`frontend/src/pages/Runs.tsx:447`）。
- trainer 镜像按 toolkit revision 构建（`pipelines/trainer.py:38`）。改了 toolkit 会自动触发重建，已有的 run 不受影响。

## 失败分类

| 类别 | 判定（按顺序匹配） | 处理 |
|---|---|---|
| **model**：模型自身行为 | 网关对该 sid 返回过 context-limit 错误（`adapters/common.py:416` 分支）；或最后一轮 `finish_reason == "length"`；或超时（仅当 `timeout_policy=penalize`；默认 `drop`，见 D1） | 保持现状：reward=0，参与训练 |
| **transient**：瞬时基础设施故障 | `invoke_async` 抛异常（boto3 重试 5 次后仍失败、限流、5xx、冷启动）；轮询 S3 时出错；`status_code=500` 且该 sid **没有捕获到任何模型轮次** | 换新 sid 重试，最多 `max_rollout_retries` 次（默认 1）；仍失败则丢弃 |
| **agent_error**：agent 或环境代码异常 | `status_code != 200`，但不属于以上两类（已有模型轮次的 traceback，比如 IndexError） | 丢弃：抛异常，不训练、不重试（这类 bug 大多是确定性的，重试只会拖长同步 step） |
| contract | reward 不是数字 | 不变：抛异常 |

## Requirements

- **R1 分类（toolkit）**：
  - 网关给每个 session 记一个 `context_exhausted` 标记，在发出 context-limit 错误时置位，并在 `finish_session` 之前提供读取。
  - `AgentCoreAgentLoop` 根据上表把失败归入 `model` / `transient` / `agent_error`，不靠匹配错误文本。
  - 最初约定单条失败日志包含 `class/action/phase/sid/step/reason`。最终交付的日志不含 `phase`，TuningPad 改用仅统计训练 rollout 的每步指标，不解析这些单条日志；最终契约以 R6 和下文“Toolkit 交付”为准。
- **R2 丢弃（toolkit）**：`agent_error` 以及重试用完的 `transient`，统一抛出带分类信息的 `RolloutDropped`（继承 `RuntimeError`）。依赖 verl sync 模式的默认行为，同组其他样本照常训练，`total_missing_sessions` 会算上这些丢弃。
- **R3 重试（toolkit）**：
  - 只重试 `transient`。重试时先 `finish_session` 丢弃旧 session，再 `create_session`，并用新 sid 重新 invoke，退避 2–5 秒（带随机抖动）。
  - 首次尝试和所有重试**共用同一个** `max_rollout_time` 截止时间，保证同步 step 的最坏耗时不超过现状。
- **R4 配置（toolkit + TuningPad）**：
  - 新增 agent loop 参数：`drop_agent_errors`（默认 true）、`max_rollout_retries`（默认 1）、`timeout_policy`（`drop`｜`penalize`，默认 `drop`）。
  - TuningPad 的 `agent_loop_yaml` 只在 template 的 `agent_loop` 设置了这三个参数时才透传，其余情况用 toolkit 自己的默认值（已实现）。它们不加进 `AGENT_LOOP_DEFAULTS`，因为那组参数会显示在控制台表单上。本期不在控制台开放这些设置。
- **R5 保护（toolkit trainer mixin）**：
  - 如果某个 step 的丢弃比例（`total_missing_sessions` 除以名义 rollout 数）连续 `K=3` 步超过 `max_drop_fraction`（默认 0.5），直接让训练失败，报错信息里写明分类计数，避免确定性 bug 让训练在几乎没有数据的情况下继续跑、白烧 GPU。
  - TuningPad 识别到这个错误后直接让 run 失败，不走 RayJob 重试（见 R6b）。
- **R6 可观测（TuningPad，已实现）**：
  - 使用 toolkit 在训练日志行里上报的每步指标：`training/rollout_failure/total_<class>_<action>`（每步都有，没有时为 0）和 `drop_fraction`。这些指标只统计训练阶段的 rollout。Runs 页的 health 图展示 `total_missing_sessions`（丢弃数）、`total_transient_retry` 和 `total_model_train`；其他指标照常入库。
  - 不解析单条 `[rollout-failure]` 日志行。原因有三：Ray 默认的日志去重会把只差 sid、step 的行合并，导致少计；toolkit 最终的日志格式里没有 `phase` 字段；每步指标已经包含了同样的信息。
- **R6b 保护触发后不重试（TuningPad，已实现）**：RayJob 失败时，如果最新一次尝试的日志里出现 `RolloutFailureGuardError`，run 直接以 `run.rollout_failure_guard` 失败，不再从 checkpoint 恢复。恢复后同一个 bug 会再触发一次保护，每次重试都要白跑 `agentcore_drop_guard_steps` 步 GPU。`pipelines/run.py:_non_retryable` 只检查最新一次尝试的日志，旧尝试里的保护日志不影响重试。
- **R7 修复已知 bug（toolkit 示例）**：`examples/strands_officebench_agent/rl_app.py:98` 遇到空内容时安全取文本，保证 reward 一定会计算。OfficeBench agent 镜像需要在控制台手动重建：agent 镜像不按 toolkit 版本自动重建，trainer 镜像会自动重建。

## Toolkit 交付（2026-10-10，已合并）

- `feat/rollout-failure-policy` 的实现提交 `2788cd3` 已通过 PR #2 合并至 `main`（`642471f`）。收尾时已用本地 Git 日志和祖先关系核实；原先“未 push”的描述是交付当时的状态，不再是待办。

- loop 参数：`drop_agent_errors=True`、`max_rollout_retries=1`、`timeout_policy="drop"`。丢弃时抛 `RolloutDropped`，异常带 `.failure_class`。
- 保护：`RolloutFailureGuardMixin`（仅 `agentcore_sync`）。超阈值时抛 `RolloutFailureGuardError`。阈值配置为 `+trainer.v1.agentcore_max_drop_fraction`（默认 0.5，设为 null 关闭）和 `+trainer.v1.agentcore_drop_guard_steps`（默认 3）；这两个 key 不在 verl 原有配置里，所以要加 `+` 前缀。TuningPad 目前不传，使用默认值。
- 日志行（不含 `phase`）：`[rollout-failure] class=… action=… sid=… step=… reason=…`。worker 日志确实会转发到 driver（`RAY_LOG_TO_DRIVER` 默认开启），但同样受 Ray 去重影响。
- 网关：新增 `RolloutGateway.context_exhausted(sid)` 和 `TrajectoryManager.last_finish_reason(sid)`。
- 对方报告全量测试 702 passed、17 skipped。本项目独立复跑 `tests/backends/verl`，138 passed。真实 Ray 环境下的统计 actor 只用假 handle 测过。

## Acceptance Criteria

- [x] AC1：toolkit `tests/backends/verl/test_agent_loop.py` 新增用例，覆盖四类失败的判定和处理：context-limit 仍是 reward=0 并产出行；IndexError 型 500 抛出 `RolloutDropped(class=agent_error)` 且只 invoke 一次；invoke 异常重试一次，成功后正常产出；重试共用截止时间；`timeout_policy=drop` 生效。
- [x] AC2：mixin 测试覆盖：丢弃比例连续 3 步超过阈值时报错，未连续超阈值时不报错。
- [x] AC3：TuningPad `tests/test_runs.py` 断言新参数能从 template 透传到 `agentcore_agent.yaml`，且不改默认 Hydra overrides；覆盖保护触发后不重试、旧尝试的保护日志不阻止重试；`tests/test_metrics.py` 覆盖 toolkit 每步指标的解析；`make verify` 通过。
- [x] AC4：OfficeBench 修复后，构造空 content 的 `response` 单测，确认 reward 会被计算。
- [ ] AC5（真实环境，需单独批准费用）：用一个小规模 run 验证。比如 GSM8K、Qwen3.5-2B、1–2 步，并人为注入一个只影响部分 payload 的 agent 异常。日志里应出现 `class=agent_error action=drop`，同组其余样本仍参与训练，health 图有对应序列，step 正常完成。

## Out of Scope

- 异步训练模式（`agentcore_*_async`）。异步模式下 verl 会剔除整个失败组，需要另外设计。
- 评测路径：评测不走 agent loop，失败样本已在 Evals 页单独展示。
- 在控制台开放这些设置的界面。

## Decisions

- **D1（2026-10-10，用户）**：超时直接丢弃，`timeout_policy` 默认为 `drop`；`penalize` 作为可选项保留。这样 model 类只剩 context-limit 和 `finish_reason == "length"` 两种情况。
- **D2（2026-10-10，用户）**：toolkit 部分（R1、R2、R3、R4 的 toolkit 侧、R5、R7，以及 AC1、AC2、AC4）交给本机 `../agentcore-rl-toolkit` 项目里的 Darwin 实现。TuningPad 侧（R4 透传、R6，以及 AC3）由本项目完成。接口约定以日志行格式和参数名为准。
