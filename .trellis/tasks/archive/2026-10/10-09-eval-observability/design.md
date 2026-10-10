# Design: 评估结果可观测性

## 1. 总体架构

```
新建评测（observe=true）
  └─ eval.run 作业
       ├─ stage observability：确认 Transaction Search 已开启；找到 / 部署 / 更新
       │    评测专用 runtime（训练 runtime 的同一镜像 + OTEL 环境变量）
       └─ stage evaluate：RolloutClient.run_batch 改用评测专用 runtime 的 ARN；
            每个样本的 runtimeSessionId 写进 results.jsonl 的 session_id 字段

AgentCore runtime（TP_OBSERVABILITY=1）
  └─ opentelemetry-instrument python -m rl_app → Strands span → X-Ray → aws/spans

评测详情页
  ├─ GET /api/evals/{id}/samples             ← results.jsonl（本地缓存，缺失时从 S3 取）
  ├─ GET /api/evals/{id}/samples/{index}     ← 同上 + 数据集该行的任务输入
  └─ GET /api/evals/{id}/samples/{index}/trace ← Logs Insights（session.id）→ span 树
```

分层：路由保持很薄（`routers/serving.py` 新增 3 个评测接口，新增 `routers/observability.py` 两个接口）；逻辑放在新文件 `services/observability.py`（Transaction Search、Logs Insights 查询、span 规范化与建树）和 `services/serving.py`（样本规范化与脱敏）；评测专用 runtime 的部署复用 `services/agents.py:deploy_runtime`。

## 2. 评测专用 runtime

镜像（两个模板一起改）
- `templates/*/agent/Dockerfile`：`strands-agents[openai,otel]==1.18.0` + `aws-opentelemetry-distro==0.21.0`（精确版本，按 AGENTS.md 固定）。新增 `tp_entry.sh`：`TP_OBSERVABILITY=1` 时 `exec opentelemetry-instrument python -m rl_app`，否则 `exec python -m rl_app`。训练 runtime 不设这个变量，进程与现在完全一样；镜像会变大，需要重建 agent 镜像后才可用。
- 用户上传的 zip / ECR 镜像不改；评测专用 runtime 仍可部署，但只有镜像自己插桩才会有 span。

数据模型（不需要迁移）
- `AgentRuntime` 有 `(agent_id, cluster_id)` 唯一约束，SQLite 无法修改。评测专用 runtime 不新增行，而是作为同一行的新 JSON 列 `obs`（可空，`init_db` 自动追加）：`{runtime_id, runtime_arn, image_uri, status, platform_version, error}`。
- `Eval` 新增可空列：`observe`（bool）、`obs_runtime_arn`、`started_at`、`ended_at`（用于 Logs Insights 时间窗）。

部署
- 名称 `<训练 runtime 名>_obs`（长度仍在 48 字符内，超出时截断基名）。参数与训练 runtime 相同（镜像、角色、VPC 网络、协议、生命周期、platformVersion），额外传 `environmentVariables`：
  - `TP_OBSERVABILITY=1`、`AGENT_OBSERVABILITY_ENABLED=true`
  - `OTEL_PYTHON_DISTRO=aws_distro`、`OTEL_PYTHON_CONFIGURATOR=aws_configurator`
  - `OTEL_RESOURCE_ATTRIBUTES=service.name=<obs 名>`
  - `UNIFIED_TRACES_DESTINATION_ENABLED=false`：span 落到 `aws/spans`（split 模式）。unified 模式要求给 agent 执行角色加 `logs:PutResourcePolicy`，这会扩大角色权限，暂不采用。
  - AgentCore 是否会自动注入其余 OTEL 变量（如 logs exporter headers）需要实测，见 §6。
- `stage_observability`（幂等）：读 `obs` → 没有就创建；镜像与训练 runtime 不一致就更新；等待 READY（复用 agent.deploy 现有的等待与 AZ 重试逻辑）；不跑契约冒烟（训练 runtime 已经冒烟过，镜像相同）。
- 清理：`delete_runtime` / 删除 agent / 删除集群（已有“先删 VPC runtime 再等 ENI”的顺序）都要同时处理 `obs.runtime_id`。

权限
- ACR 执行角色已有 X-Ray `PutTraceSegments` / `PutTelemetryRecords` / `GetSampling*` 和 `/aws/bedrock-agentcore/runtimes/*` 日志写权限，split 模式不需要新增。
- 后端本机凭证需要 `logs:StartQuery`、`logs:GetQueryResults`、`logs:StopQuery`、`xray:GetTraceSegmentDestination`；开启时还需要 `xray:UpdateTraceSegmentDestination`、`logs:PutResourcePolicy`。把读权限加进 preflight 的 `REQUIRED_ACTIONS`。

## 3. Transaction Search

- `GET /api/observability/status?region=` → `{region, transaction_search: "active" | "pending" | "off", destination}`，读 `xray.get_trace_segment_destination`。
- `POST /api/observability/transaction-search {region, confirm: true}`：
  1. `logs.put_resource_policy`：策略名 `TuningPadTransactionSearch`，允许 `xray.amazonaws.com` 对 `aws/spans:*` 和 `/aws/application-signals/data:*` 执行 `logs:PutLogEvents`，条件限定 `aws:SourceAccount` 与 `aws:SourceArn=arn:aws:xray:<region>:<account>:*`；
  2. `xray.update_trace_segment_destination(Destination="CloudWatchLogs")`。
  - 不改 indexing 规则：span 全量写入 `aws/spans`，按 session.id 用 Logs Insights 查询不依赖索引比例。
  - 没有 `confirm: true` 时返回 400 `observability.confirm_required`；已开启时直接返回，不重复写。
- 创建评测时 `observe=true` 但该区域未开启 → 409 `eval.observability_off`（detail 带 region），前端据此弹确认框；用户确认后先调开启接口，再重新提交。
- 这是账号级设置（开启后该区域所有 X-Ray trace 都写入 CloudWatch Logs），确认框里写清楚；关闭不在本任务范围内（需要时在 X-Ray 控制台改回）。

## 4. 样本与调用链 API

会话关联
- results.jsonl 每行的 `result.result_key` 是 `evals/<eval>/<input_id>/<session_id>.json`（实测 22/22 行都有），从文件名取 `session_id`，toolkit 不用改。调用本身失败、没有结果文件的样本没有 session，调用链页显示“无调用记录”。
- `stage_eval` 写 results.jsonl 时顺便把 `session_id` 提到行的顶层，并记录 `Eval.started_at` / `ended_at`。

`GET /api/evals/{id}/samples?status=all|failed|truncated&offset=&limit=`（limit ≤ 100）
- 读 `<data_dir>/datasets/<eval-id>/results.jsonl`，本地没有就从 `results_s3` 下载缓存。按 `index` 排序。
- 每行返回 `{index, state, reward, status_code, stop_reason, turns, tool_calls, elapsed_s, session_id}`；`state` ∈ `scored | truncated | acr_failed | invoke_failed`，判定规则与 `summarize_eval` 一致（新增测试保证两者计数相同）。
- 响应带 `{total, counts: {scored, truncated, acr_failed, invoke_failed}, observe}`。

`GET /api/evals/{id}/samples/{index}`
- `input`：数据集该行的 `prompt` 列（有显式 prompt 列时）或 payload 中的可读字段（如 OfficeBench 的 `task`），payload 中的 URI 类字段原样显示，其余字段不外发。
- `messages`：规范化为 `[{role, blocks: [{type: text|tool_use|tool_result|reasoning, text?, name?, tool_use_id?, input?, status?, truncated?}]}]`；单块文本上限 20,000 字符、消息上限 200 条，超出标 `truncated`。
- `result`：`reward`、`status_code`、`stop_reason`、`traceback`（截断到末尾 8,000 字符）。
- 脱敏：只按白名单字段组装，`payload`、`_rollout`、`s3_bucket`、`result_key` 不外发；再对所有字符串做一次键名/形态检查（`api_key`、`tp-` 开头的推理 key、AWS 凭证格式），命中则替换为 `***`。

`GET /api/evals/{id}/samples/{index}/trace`
- 仅 `observe=true` 的评测可用，否则 404 `eval.trace_unavailable`。
- Logs Insights（`logs.start_query` / `get_query_results`，`aws.client("logs", region)`）：
  ```
  SOURCE logGroups(namePrefix: ['aws/spans', '/aws/bedrock-agentcore/runtimes/'])
  | filter attributes.session.id = "<session_id>" and ispresent(startTimeUnixNano)
  | fields @message | limit 500
  ```
  时间窗 `[eval.started_at - 5min, (eval.ended_at or now) + 30min]`；session_id 先按 UUID 格式校验再拼进查询。最长等 55 秒，限流重试一次。
- 规范化（移植 launchpad `build_span_tree` / `categorize_span` / `parse_message_events` 的思路，重写为本仓库代码）：
  `span = {span_id, parent_id, name, category: agent|llm|tool|other, start_ms, duration_ms, offset_pct, width_pct, depth, status, model, tokens: {input, output}, tool, input_messages, output_messages, attributes}`；父 span 缺失则挂到根；按开始时间排序；内容来源依次尝试 span events（`gen_ai.user.message` / `gen_ai.choice`）、`gen_ai.input/output.messages` 属性；属性中疑似密钥的值脱敏。
- 返回 `{state: ready|pending|empty, trace: {duration_ms, totals: {llm_calls, tool_calls, tokens}, spans: [...]}}`。查不到 span 时：评测结束不到 15 分钟 → `pending`（前端提示稍后刷新），否则 `empty`。
- 60 秒进程内缓存（键 = eval + index），`?force=1` 跳过；查询失败 → 502 `observability.query_failed`。

实现后的接口契约（`services/observability.py:sample_trace` / `build_trace`，2026-10-09）：
```
{state: "ready" | "pending" | "empty", session_id?, reason?: "no_session" | "no_spans",
 trace?: {duration_ms, started_at, totals: {spans, llm_calls, tool_calls, errors,
          input_tokens, output_tokens},   # tokens = LLM spans only (agent spans re-aggregate)
          spans: [{span_id, parent_id, trace_id, name, category: agent|llm|tool|other,
                   depth, start_ms, duration_ms, offset_pct, width_pct, status: ok|error,
                   model, tool, tool_status, tokens: {input, output},
                   input_messages, output_messages,   # transcript block format
                   attributes}]}}                       # heavy gen_ai.* blobs dropped
```
spans 按瀑布流顺序给出（深度优先，子节点按开始时间）；缺父节点的挂到根，循环引用被截断。消息解析同时支持 Strands 1.18 默认的事件格式（`gen_ai.{user,assistant,tool}.message.content` / `gen_ai.choice.message`）和最新约定的 parts 格式（`gen_ai.input/output.messages`）。评测专用 runtime 设置了 `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`（ADOT 0.21 README 推荐），内容留在 span 上。

## 5. 前端

- `pages/Evals.tsx`：列表行加“详情”按钮 → `?view=detail&id=`；新建表单加开关“记录调用链（瀑布流）”与说明文字（首次会自动部署评测专用 runtime，需要几分钟）。提交收到 `eval.observability_off` 时弹 `Confirm`：说明 Transaction Search 是账号级设置，确认后调用开启接口并重新提交。
- 新文件 `pages/EvalDetail.tsx`：
  - 顶部 `Kpi`：平均 reward、打分 / 截断 / ACR 失败 / 调用失败计数、是否开启调用链。
  - 样本表（`Table` + `Pager`，`Segmented` 切换 全部 / 失败 / 截断）：序号、状态 `Tag`、reward、stop_reason、轮数、工具次数、耗时。
  - 点击行打开 `Drawer`（宽版），内含 `SubTabs`：“对话” / “调用链”（未开启调用链时只显示“对话”）。
- 新组件 `components/Transcript.tsx`：任务输入卡片；逐轮消息（用户 / 助手气泡；工具调用卡片显示工具名 + 参数 `v2-pre`；工具结果显示状态 `Tag` + 内容，长内容默认折叠）；失败时 `Alert` + traceback。
- 新组件 `components/TraceWaterfall.tsx`：左右两栏（窄屏上下排列）。左侧每行 = 缩进（`depth×14px`）+ 类别色点 + 名称 + 时间条（`left/width` 百分比，最小 0.4%）+ 耗时；顶部时间刻度与合计（LLM 调用数、工具调用数、token）。右侧选中 span 的详情：类别、模型、耗时、token、工具名、输入/输出消息、属性（默认前 12 条，可展开）。键盘 ↑/↓ 切换 span。颜色沿用 launchpad：agent `#14c9c9`、LLM `#1664ff`、工具 `#ff7d00`。`pending` / `empty` / 错误分别显示说明与“刷新”按钮。
- 样式放在 `app.css` 的 `.v2 .tp-wf*`、`.v2 .tp-msg*`，只用 v2 已有的变量；不引入新依赖。
- 所有文案走 `t()`，`evals.detail.*`、`evals.trace.*`、`apiErrors.*` 新键同时写进 en / zh-CN；接口全部加到 `lib/api.ts` 的 `servingApi` / 新 `obsApi`。

## 6. 风险与待验证

实测结论（第 0 步探针，2026-10-09，us-east-1，rl-dev-ue-1，OfficeBench，Bedrock gpt-oss-20b 契约冒烟 2 条）：
- Transaction Search 在该账号 us-east-1 已经是 ACTIVE（先前由他人开启，策略名 `TransactionSearchXRayAccess`，`aws/spans` 保留 30 天，索引 1%）；开启接口走了“已开启不再写”的分支。按 session.id 查 `aws/spans` 不受 1% 索引比例影响。
- 新镜像（ADOT 0.21 + `strands-agents[otel]` + `tp_entry.sh`，481 MB）用于训练 runtime 时契约冒烟 2/2 通过（未设 `TP_OBSERVABILITY`，进程不变）；用于评测专用 runtime（V2 平台）时冒烟 2/2 通过。
- span 在 3 分钟内可查。Strands span 落在 `aws/spans`，`service.name` = 我们设置的 `<名称>_obs`，每个 Strands span 都带 `session.id`。runtime 自己的日志组里只有 EMF 指标记录（无 `startTimeUnixNano`），被查询的 `ispresent(startTimeUnixNano)` 过滤掉。
- `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` 有效：消息内容作为 span events 保留在 `aws/spans`（`gen_ai.{user,assistant,tool}.message.content`、`gen_ai.choice.message`；循环 span 的 `gen_ai.choice` 另有 `tool.result`）。作为对照，同账号里其他项目使用默认设置的 span 完全没有 events。
- 一次 OfficeBench rollout 的真实树：`POST /invocations` → S3.GetObject / `invoke_agent` → `execute_event_loop_cycle` → `chat`（下挂模型 HTTP POST）/ `execute_tool calendar_create_event` → … → S3.PutObject；LLM span 的 token 之和等于 `invoke_agent` 自带的汇总（4044/295）。该 rollout 27 秒里工具调用占 22.8 秒，正是瀑布流要暴露的信息。
- 由此修正：执行工具 span 的 `gen_ai.choice` 解析为工具结果、循环 span 读取 `tool.result`、`exception` 事件（如 `MaxTokensReachedException`）标红并展示类型/消息/调用栈。真实记录（脱敏）存为 `backend/tests/fixtures/strands_officebench_spans.json` 并有回归测试。
- 顺带发现并修复：OfficeBench 契约冒烟按“同模板最新数据集”取 payload，没有限定区域，导致 us-east-1 runtime 去读 us-east-2 的桶（AccessDenied）；现在按 runtime 区域选数据集。

仍未验证：
1. 冷启动时间变化（评测专用 runtime 首次部署约 5 分钟，主要是 V2 快照与 AZ 重试；镜像变大对训练 runtime 拉取时间的影响未单独测量）。
2. 完整的“开启调用链的评测 → 详情页瀑布流”端到端（AC4，第 7 步）：需要 g5 推理端点，本次 us-east-1 g5 按需容量不足未起来。

已知取舍：
- split 模式（`aws/spans`）而不是 unified：不给 agent 执行角色加 `logs:PutResourcePolicy`。代价是不跟随 2026-07-20 后的默认模式；查询同时覆盖两种日志组前缀，以后切换不影响读取。
- Transaction Search 开启后对整个区域生效，我们不自动关闭；确认框中说明。
- 训练 runtime 与评测专用 runtime 共用镜像：训练镜像会包含 ADOT 依赖，但不启用。
- 回滚：关闭开关即可回到现状；评测专用 runtime 可以单独删除；样本与对话接口只读本地 / S3 结果，不影响现有流程。
