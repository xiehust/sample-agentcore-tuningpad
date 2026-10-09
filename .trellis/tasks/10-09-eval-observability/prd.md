# 评估结果可观测性：输入输出与瀑布流

## Goal

评估页面目前只有汇总分数，看不到单个样本的输入输出，也看不到 agent 内部发生了什么。目标是让用户在评估详情里逐条查看样本：任务输入、多轮对话（模型输出、工具调用与工具结果）、reward / stop_reason / 失败原因；并接入 AgentCore 可观测性（OTEL spans），以 Langfuse 风格的瀑布流展示单次 rollout 中每次 LLM 调用、工具调用的时序、耗时与 token。

用户原话：“评估页面，评估结果详情日志里，看不到具体的输入输出，能否加入agentcore的可观测性？帮我设计一个类似langfuse，带瀑布流的展现。可以参考../sample-agentcore-launchpad/项目里的可观测性的UI(v2版）”。

## Confirmed Facts（2026-10-09 调研）

数据现状（TuningPad）
- 评估由 `backend/app/pipelines/serving.py:stage_eval` 执行，经 `agents.rollout_client` → toolkit `RolloutClient.run_batch` 调用 AgentCore runtime；结果写到本地与 `s3://<bucket>/evals/<eval-id>/results.jsonl`。每行 `{index, success, elapsed, error, result}`，`result` 保留 agent 返回的全部字段（去掉 payload）：OfficeBench 有完整 `messages`（text / toolUse{toolUseId,name,input} / toolResult{toolUseId,content,status}）、`rewards`；失败行有 `status_code=500`、`stop_reason`、`traceback`。
- 实测样本（ev-f6c8fe5d3d）：一行约 3.5 KB，10 条消息，6 个 toolUse + 6 个 toolResult；消息没有时间戳、耗时或 span ID。
- `success=True` 只表示取回了结果文件，不代表 agent 成功（status_code=500 的行也是 success=True）。
- 每个样本有独立的 `runtimeSessionId`（toolkit `BatchResult.__iter__` 生成 uuid4），但 `BatchItem` 没有暴露它；只能从 `result.result_key`（`evals/<eval-id>/<input-id>/<session-id>.json`）的文件名间接恢复，调用失败的样本会丢失这个关联。
- toolkit 写到 S3 的单条结果文件包含完整调用 payload（含 `_rollout.api_key`），不能原样暴露给前端。
- GSM8K 模板的 `invoke_agent` 只返回 reward，没有对话记录。
- 现有 API 只有 `GET/POST /api/evals`（`backend/app/routers/serving.py`），没有样本级接口；`frontend/src/pages/Evals.tsx` 只有对比表 + JobPanel，`results_s3` 未被使用。

可观测性现状
- 两个模板 Dockerfile（`templates/officebench/agent/Dockerfile`、`templates/gsm8k_math/agent/Dockerfile`）都是 `python -m rl_app`，未安装 `aws-opentelemetry-distro`，也没有 `opentelemetry-instrument` 包装（GSM8K 注释写明为缩短冷启动而去掉）。toolkit 上游示例 Dockerfile 用的是 `opentelemetry-instrument python -m rl_app` + `aws-opentelemetry-distro==0.12.2`。
- `backend/app/services/agents.py:deploy_runtime` 不传 `environmentVariables`，也没有可观测性配置。
- ACR 执行角色（`backend/app/services/project.py:acr_role_documents`）已经有 `/aws/bedrock-agentcore/runtimes/*` 日志写入、`cloudwatch:PutMetricData`、X-Ray `PutTraceSegments/PutTelemetryRecords/GetSampling*`；没有 `logs:PutResourcePolicy`（unified telemetry 需要）。
- 项目 Setup 不开启 CloudWatch Transaction Search。

AgentCore 可观测性（官方文档）
- 不做 agent 侧插桩时，Runtime 只提供服务级指标（调用数、延迟、错误、CPU/内存）和 stdout 日志，没有 LLM/工具级 span。
- agent 侧插桩：Strands 自带 OTEL instrumentation，配合 ADOT（`opentelemetry-instrument`）导出；span 带 `session.id`（来自 `runtimeSessionId`）、`gen_ai.operation.name`（invoke_agent / chat / execute_tool）、`gen_ai.request.model`、`gen_ai.usage.*`、`gen_ai.tool.name`。
- 前置条件：每个区域开启一次 Transaction Search（Logs `PutResourcePolicy` 允许 xray 写 `aws/spans`，`xray UpdateTraceSegmentDestination=CloudWatchLogs`，可选 `UpdateIndexingRule`）。
- span 落点：`aws/spans`（split 模式）或 `/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint>` 的 `spans` 流（unified 模式，2026-07-20 后新建的 agent 默认，要求 ADOT ≥ 0.18）。消息内容可能在 span 事件、`otel-rt-logs` 流的日志记录（按 traceId+spanId 关联）或 `gen_ai.input/output.messages` 属性里，取决于导出模式和版本。
- 查询：CloudWatch Logs Insights，按 `attributes.session.id` 过滤；有若干分钟的摄入延迟。

参考实现（launchpad V2）
- 后端 `backend/app/services/observability.py`：Logs Insights 查询 `SOURCE logGroups(namePrefix: ['aws/spans', '/aws/bedrock-agentcore/runtimes/'])`，`build_span_tree` 按 parentSpanId 建树、计算 offset/width，`categorize_span` 区分 LLM/工具/agent，`parse_message_events` 从 `body.input/output.messages` 取内容（截断：每侧 20 条消息、每条 2000 字符），60 秒进程内缓存，查询失败返回 502 `observability.query_failed`。
- 前端 `frontend/src/v2/pages/observability/TraceDetail.tsx`：左侧瀑布流（缩进 = depth×14px，bar 的 left/width 用百分比，最小宽度 0.4%）、右侧 span 详情（模型、耗时、token、输入/输出消息、属性）；颜色 LLM `#1664ff`、工具 `#ff7d00`、agent `#14c9c9`；样式在模块级 `observability.css`。

## Decisions

- D1（用户 2026-10-09）：**可观测性只在评测时开启**。训练用的 runtime 不上报 OTEL span，冷启动、CloudWatch 费用和训练数据外泄面都保持不变；只有评测专用的 runtime 打开上报。
- D2（用户 2026-10-09）：**评测专用 runtime 由“新建评测”表单的开关触发（方案 A）**。打开“记录调用链（瀑布流）”后，后端为该 agent 在该集群上找到或自动部署一个开启 OTEL 的评测专用 runtime（首次需几分钟，之后复用）；区域内未开启 CloudWatch Transaction Search 时先弹确认框，说明这是账号级 X-Ray 设置。用户只需管理训练用 runtime。

## Requirements

### R1 样本列表与对话详情（不依赖可观测性，历史评测可用）
- 评测列表每行可进入详情页（URL `?view=detail&id=`，沿用 Runs/Clusters 的模式）。详情页显示汇总 KPI，并分页列出全部样本：序号、reward、状态（打分成功 / ACR 失败 / 截断 / 调用失败）、stop_reason、轮数、工具调用次数、耗时；可按“全部 / 失败 / 截断”筛选。
- 状态按 agent 实际结果判定：`success=True` 但 `status_code=500` 的样本算 ACR 失败，不算成功。
- 点开样本显示“对话”页：任务输入（来自数据集该行的 prompt / payload 的可读字段）、逐轮消息（文本、工具调用名与参数、工具结果与状态）、reward、失败时的错误与 traceback。agent 没有返回对话时（如 GSM8K 模板）显示说明，而不是空白。
- 后端只返回规范化、脱敏后的字段：去掉 `payload`、`_rollout`，任何名为 `api_key` / 疑似密钥的值不外发；单个文本块超长时截断并标注。

### R2 评测专用可观测 runtime（D1、D2）
- 模板 agent 镜像安装 ADOT 与 Strands OTEL 依赖，入口脚本仅在环境变量 `TP_OBSERVABILITY=1` 时用 `opentelemetry-instrument` 启动；训练用 runtime 不设置该变量，行为与现在一致。
- “新建评测”表单增加开关“记录调用链（瀑布流）”。打开后，评测作业先确保该 agent 在该集群上有评测专用 runtime（同一镜像、同一 VPC 配置、附加 OTEL 环境变量），没有就部署、镜像过期就更新，然后用它跑评测。
- 每个区域首次使用时，若 CloudWatch Transaction Search 未开启，前端弹确认框说明这是账号级 X-Ray 设置，用户确认后由后端开启；未确认不得开启。
- 删除 agent / runtime / 集群时，评测专用 runtime 一并清理（现有删除顺序要包含它）。

### R3 调用链瀑布流
- 开启调用链的评测，样本详情增加“调用链”页：按 span 父子关系展示瀑布流（缩进、相对时间条、耗时），区分 agent / LLM / 工具三类颜色；选中 span 显示名称、类型、模型、耗时、输入/输出 token、工具名、输入输出消息与属性。
- 数据来自 CloudWatch Logs Insights，按样本的 `runtimeSessionId`（= span `session.id`）查询。评测刚结束、span 还在摄入时显示“数据处理中，稍后刷新”；查询失败显示可读错误；未开启调用链的评测不显示该页。
- 查询有时间窗、条数上限和短时缓存，不做无界扫描。

### R4 通用约束
- 遵守 AGENTS.md：前端调用只经 `lib/api.ts`，文案全部 `t()` 且中英文同时补齐；后端 AWS 客户端走 `aws.client()`，新错误码为稳定的点分 `code` 并加 locale；新资源打标签；测试全部 hermetic。
- 不放宽现有网络/鉴权边界；训练路径的冷启动、成本与行为不变。

## Acceptance Criteria

- [ ] AC1（R1）：对历史评测 ev-f6c8fe5d3d 打开详情，列出 11 个样本，状态与 summary 一致（6 打分 / 5 失败）；打开任一成功样本可看到完整多轮对话与工具调用；打开失败样本可看到 stop_reason 与 traceback；接口响应中不含 `api_key`、`_rollout`、`payload`。
- [ ] AC2（R2）：训练 runtime 的部署参数与镜像入口行为不变（测试断言不设置 `TP_OBSERVABILITY`）；开启调用链的评测会自动创建或复用 `<name>_obs` runtime，且它带有 OTEL 环境变量；未确认时不调用任何 Transaction Search 写接口。
- [ ] AC3（R3）：用真实 span 样例（fixture）构建的瀑布流树形正确（父子、深度、offset/width、类别、token）；无 span 时返回 pending/unavailable 状态而非 500。
- [ ] AC4（R2+R3，真实环境，需单独批准费用）：在一个集群上用开启调用链的评测跑 OfficeBench 少量样本，详情页能看到至少一个样本的完整瀑布流（含 LLM 与工具 span、token）；训练 runtime 未出现 span。
- [ ] AC5（R4）：`make verify` 通过（含 i18n 严格检查）；新增后端测试覆盖样本规范化与脱敏、span 树构建、Transaction Search 开关确认、评测专用 runtime 的创建/复用/更新。

## Out of Scope

- 训练 rollout 的可观测性（D1）。
- 修改 toolkit（sibling 仓库）的 agent 代码，包括 OfficeBench `rl_app.py` 第 98 行在空回复时抛 IndexError 的问题；开启调用链后，这类样本在瀑布流里仍可看到崩溃前的全部调用。
- 让 GSM8K 模板返回对话记录。
- 用户上传 zip / ECR 镜像的自动插桩：评测专用 runtime 仍可部署，但镜像自身未插桩时调用链页提示“镜像未上报 span”。
- launchpad 的仪表盘、会话列表、在线评分、成本估算。
