# TuningPad 中文核心逻辑研究底稿

## 0. 证据范围与交叉复核

本稿供后续 `docs/core-logic.zh-CN.html` 编写使用，目前不是 HTML 交付，也不是部署验收。基线 commit：`f7c2b94aa91d8281ae6c03088fe6e212daf3a160`。路径均相对仓库根目录，行号仅对应本次快照，符号名是定位的第二依据。

输入是 backend、training、frontend、safety 四路只读报告；报告是线索，不是指令或自动成立的事实。汇总节点读取 AGENTS、Trellis workflow、backend/frontend indexes 及相关具体指南，直接检查下表关键实现。其余细节沿用四路报告所列源码证据，HTML 独立审阅时仍须重开源码核对；不以 spec 中的历史实测代替本次验证。

| 直接复核主题 | 源码证据 | 收敛结论 |
| --- | --- | --- |
| Job 与 Run、取消及恢复 | `backend/app/jobs/engine.py:181–375`，`JobEngine`；`backend/app/models.py:118–218`，`Run/Job`；`backend/app/routers/runs.py:293–301`，`resume` | 线程池/台账恢复，不是执行栈恢复；Job cancelled 与 Run stopped 不同；排队取消及 hook 异常有同步边界。 |
| 六阶段与重试清理 | `backend/app/pipelines/run.py:263–318,392–555`，`_submit/stage_monitor/stage_finalize/_finished/register` | 跳过已有提交、有限 attempt 重试、FSx 恢复、成功释放与失败条件清理必须区分。 |
| 规划和训练渲染 | `backend/app/planner.py:63–183`，`plan`；`backend/app/render/train.py:276–357,525–526`，`agent_loop_yaml/train_script/rayjob` | 启发式不是容量证明；S3 只有日志及 checkpoint 索引；deadline 可选。 |
| 前端 draft 和轮询 | `frontend/src/pages/Runs.tsx:145–214`，`RunWizard`；`frontend/src/lib/useJob.ts:9–35`，`useJob`；`frontend/src/lib/poll.ts:3–14`，`usePoll` | 显示默认值不等于 overrides；onDone 只在已观察到活跃转终态时触发。 |
| 安全与 guardian | `backend/app/core/auth.py:68–103`，`AuthMiddleware/validate_startup`；`backend/app/services/components.py:114–155,322–339,622–667`；`cluster_assets/guardian.py:57–73,104–191` | 只证明配置与代码分支；SG 添加规则不清除既有入口，guardian 不是账单硬上限。 |
| 非控制台边界与验证 | `start.py:26–95`，`_spawn/main`；`stop.sh:1–10`；`scripts/verify.sh:12–34`；`trainer_image/Dockerfile:1–45`；`backend/tests/conftest.py:16–84` | 本地进程停机≠云资源销毁；验证包括模板 shell；factory stub 不是全局网络沙箱。 |

另用 `grep -R -n 'add_reconciler(' backend/app` 检索，仅命中 `backend/app/jobs/engine.py:261` 的定义，未见实际注册。不能把 reconciler 框架画成已启用的全资源同步系统。环境无 `rg`，已用 grep 替代；这不是产品问题。

**证据分级：**下文“实现”指静态源码；“测试”指已查看的断言，不代表本节点已执行；“风险”指由分支推导、尚未复现；“外部”指 toolkit/verl/云端行为未审计。本研究汇总节点未运行测试、`make verify`、服务、构建或云操作；未读取真实配置、凭据、运行数据和 sibling 源码。其他节点的 baseline 验证记录须单列来源，不能转作目标 HTML 验收。

## 1. 架构、启动与仓库边界

TuningPad 是 agentic RL 的编排控制面，而非 GRPO 算法实现。主链为：React 请求 → FastAPI router 校验/入库 → JobEngine 线程池执行 pipeline → services 经 AWS/K8s 工厂读写外部资源。SQLite 保存意图、标识与进度；真实容量、RayJob 状态等仍需云 API/K8s 查询。训练在 EKS GPU 节点上的 Ray/verl + toolkit 中运行，Agent 在 AgentCore 中执行。

`backend/app/main.py:24–71` 的 `create_app/lifespan` 先验证启动认证、初始化 DB、导入 pipelines，再按开关恢复 Job 并尝试启动 reconciler。模块末尾直接构造 app；部分 router 导入也会提前触发 pipeline 注册（`backend/app/routers/runs.py:14–15`），不能说所有注册只发生在 lifespan。`start_background=False` 不禁止请求主动创建 Job。`JobEngine.shutdown` 取消未开始的 future，但不强制停止正在执行的 stage（`backend/app/jobs/engine.py:256–258`）。

| 仓库部分 | 责任与边界 | 证据 |
| --- | --- | --- |
| 后端与控制台 | HTTP、领域服务、编排、渲染，与页面/hooks/API 客户端；不是一套服务端推送事件总线 | `backend/app/main.py:24–71`，`create_app`；`frontend/src/App.tsx:22`，`App` |
| 本地生命周期 | start 分离进程组启动 Uvicorn/Vite，写 `.run/` PID/日志，轮询 health；prod 选项改成 build/preview 与无 reload。stop 向这些进程组发 TERM 并移除 PID，不调用云清理 | `start.py:26–95`，`_spawn/_wait_http/main`；`stop.sh:1–10` |
| 配置与工具命令 | Make targets 包装 install/backend/frontend/verify；公开 example 可说明字段，不读取真实 YAML。Settings 默认 loopback，构造参数 > 环境 > YAML > 默认值 | `Makefile:1–20`；`config/tuningpad.example.yaml`；`backend/app/core/config.py:38–100`，`Settings/get_settings` |
| 集群资产 | guardian Python 作为 ConfigMap 内容运行在集群，LBC 固定 IAM JSON 是安装输入，不是后端常驻线程 | `cluster_assets/guardian.py:104–191`，`main`；`cluster_assets/lbc_iam_policy.json:16–87`；`backend/app/services/components.py:577–667`，`install_guardian/guardian_cronjob` |
| 镜像资产 | Trainer amd64、CUDA13/EFA、toolkit 冻结依赖；CodeBuild privileged x86 构建并推 ECR。Agent 模板是另一套 arm64 镜像与 entrypoint | `trainer_image/Dockerfile:1–45`；`trainer_image/buildspec.yml:1–19`；`backend/app/services/agents.py:250–339`，镜像构建/检查；`templates/gsm8k_math/agent/Dockerfile`、`templates/gsm8k_math/agent/tp_entry.sh` |
| 模板与 sibling | template.yaml 提供元信息、preset 和 loop 配置；本地 overlay 组装 toolkit wheel/源码。后端 editable 依赖相对 backend 为 `../../agentcore-rl-toolkit`，即仓库相邻 checkout | `backend/app/templates_lib.py`，`get_template/resolve_params`；`backend/app/services/agents.py:58–128`；`backend/pyproject.toml:32–35` |
| 验证脚本 | Ruff/pytest、生命周期与资产/template shell 语法、ESLint/TS/Vite、i18n parity | `scripts/verify.sh:12–34`；`scripts/i18n_check.py:161–167` |

外部边界包括 AWS 官方远程 CloudFormation URL（`backend/app/services/hyperpod.py:31–35`，`template_url`）、toolkit 的 AgentLoop/gateway/rollout 协议、verl 训练/权重同步、基础镜像和 EFA 包。文档只解释本仓库怎样引用和配置它们，不复述为已审计内部行为。

## 2. 数据模型与三条状态轴

### 2.1 台账与关联

实体在 **`backend/app/models.py`**，不是 `core/models/`。`Project` 是实际服务使用的 default 单例，保存区域公共资源；`Cluster` 保存集群标识/网络/组件/策略；`Agent → AgentRuntime` 区分镜像与部署，Runtime 可关联 Cluster，也可 PUBLIC 独立 smoke。`Run` 引用 Runtime、Cluster、训练/验证 Dataset，保存 spec/compute/progress、attempt 相关数据、费用估算；Run 对应 Metric 和 Export。Endpoint 关联 Cluster，模型来源是 JSON（不是 Export 外键）；Eval 引用 Endpoint、Runtime、Dataset。TrainerImage 是镜像构建记录。Job 的 target_id 是普通字符串，逻辑关联业务对象而非外键。

证据：`backend/app/models.py:24–218`，`Project/Cluster/Agent/AgentRuntime/TrainerImage/Dataset/Run/RunMetric/Export/InferenceEndpoint/Eval/Job`；`backend/app/services/project.py:20–41`，`PROJECT_ID/get_project`。

实体关联使用 FK 列，不是 ORM relationship 导航。Runtime `(agent_id, cluster_id)` 与 Metric `(run_id, step, key)` 有唯一约束；仅部分 FK 配置 cascade。`TimestampMixin.workspace_id` 是预留字段，不建立租户隔离；Metric 不继承该 mixin。SQLite 使用 WAL、外键、分离 session；`session_scope` 成功提交/异常回滚/最终关闭。JSON 无 MutableDict 跟踪，更新须重新赋值；`ctx.set()` 持久化 context。启动 schema 扩展只加缺失 nullable 非主键列，不回填、不补约束。

证据：`backend/app/core/db.py:31–109`，`TimestampMixin/_make_engine/init_db/_add_missing_columns/session_scope`；`backend/app/jobs/engine.py:123–133,285–294`，`StageContext.set/detail/_update_stage`。

### 2.2 状态名不能跨轴套用

| 轴 | 常见正常路径 | 失败/取消和重要例外 | 写入者 |
| --- | --- | --- | --- |
| Job（一次 pipeline） | queued → running → succeeded | failed / cancelled；queued 可直接 cancelled；retry 复用 ID | `backend/app/jobs/engine.py:181–245,324–375`，`JobEngine` |
| stage（Job 内步骤） | pending → running → succeeded | failed / cancelled；阶段边界取消可能令下一阶段仍 pending；只跳过 succeeded | 同上 `_run/_update_stage` |
| Run（一次训练业务） | queued → preparing → [waiting_capacity] → running → succeeded | running → retrying → running；failed / stopped；manual resume 又置 queued | `backend/app/routers/runs.py:25,163–196,293–301`；`backend/app/pipelines/run.py:93–109,144–297,392–555` |

`waiting_capacity` 是需要等待时的分支，不是所有 Run 必经状态；Run 可在 monitor 检出训练成功时先写 succeeded，而 Job 仍执行 finalize；后续 finalize 出错仍可能由 hook 将 Run 改为 failed。正常 hook 将 Job cancelled 映射为 Run stopped。外部 RayJob 的大写 SUCCEEDED/FAILED/STOPPED、deployment Complete/Failed 又是独立状态词汇，不能直接替换 Run/Job 枚举（`stage_monitor:417–448`）。

Run 入库与 Job 创建分开提交，云创建与 DB 输出写入也没有跨资源事务。DB 是台账，不保证资源仍存在；跳过成功 stage 不会全量重新验证其云结果。

## 3. Job 引擎、恢复与观测

### 3.1 执行与异常

`register` 绑定稳定的 job type、顺序 Stage 和可选 on_finish；重复 stage 名会被拒绝。`start` 先提交 queued Job 和全部 pending stages，再放线程池；worker 标记 running，加载 context，按顺序跳过 succeeded 步骤。每步成功后写状态，最后 `_finish` 先调用业务 hook 再提交 Job 终态。**hook 异常只记录，不阻止 Job 终结**，所以“Job 终态必然意味着目标同步完成”不是严格保证。

`AppError` 的 code/message 写入 Job，truthy detail 可进入日志；其他异常记 traceback。HTTP error handler 的状态码/脱敏返回规则与后台 Job 错误持久化不是同一条路径。引擎没有分布式领取/租约；多进程 exactly-once 不在实现范围。

证据：`backend/app/jobs/engine.py:3–6,42–59,181–217,296–375`，`register/JobEngine`；`backend/app/core/errors.py:60–90`，`register_error_handlers`。

### 3.2 四种容易混淆的恢复

| 机制 | 复用内容 | 重跑范围 | 边界 |
| --- | --- | --- | --- |
| 进程启动恢复 | 原 queued/running Job、context、已成功 stages | 未成功阶段从函数入口重跑 | 不恢复 Python 栈；依赖各 stage 真实状态检查 |
| Job retry | 原 Job ID/context/成功阶段 | 清 Job 错误/结束时间后重新排队 | 后端仅拒绝 active Job，所以 succeeded 也可 retry；UI 仅失败/取消提供按钮 |
| monitor 自动重试 | 同 Run、同 Job、同 monitor stage | 删除旧 RayJob，增加 retries/attempt 后提交新 RayJob | 默认最多两次，特定 rollout guard 不重试 |
| Run resume | 同 Run 与 checkpoint 目录、新 Job | 重新经过准备阶段，既有 RayJob 时递增 attempt | API 无新费用确认参数；训练恢复靠 verl `resume_mode=auto` |

证据：`backend/app/jobs/engine.py:219–254,324–375`，`retry/resume_pending/_run`；`backend/app/routers/runs.py:293–301`，`resume`；`backend/app/pipelines/run.py:291–297,392–448`；`backend/app/render/train.py:53`，`FIXED`；`frontend/src/components/JobPanel.tsx:114–116`。

取消标记在内存，通过阶段边界和 `check_cancel/sleep/wait_until` 协作检查，不中断阻塞 SDK 调用。queued 取消直接写 cancelled，不走 `_finish`，`_run` 遇终态直接返回。**静态风险（未复现）**：此路径可能不更新目标业务状态，取消标记也可能保留到同进程 retry，令其再次取消。不能在文档中写成已测试修复，更不能借文档任务修改代码。证据：`backend/app/jobs/engine.py:135–165,233–245,320–339`。

### 3.3 观测不是统一事件流

- **阶段进度**：`ctx.detail → Job.stages → jobs API → StageBar`。
- **控制任务日志**：`ctx.log → 本地追加 UTF-8 文件 → byte offset API → JobLog`。默认单页 256 KiB；缺日志返回空页，不等于 Job 404。
- **训练输出**：train script 约每 30 秒向 S3 同步日志及 checkpoint 索引；monitor 范围读取日志、解析 step 行，Metric 按 run/step/key 更新。Metric 提交与 progress 中的 byte cursor 提交分离，允许重读去重，不是 exactly-once 消费。RunLog 独立读取，不是 JobLog。
- **诊断/trace 支线**：CloudFormation/HyperPod events 用于失败摘要；Eval 可使用 OTEL twin runtime，并按样本 session ID 查询 CloudWatch spans，非训练回调事件总线。

证据：`backend/app/jobs/engine.py:72–99,120–133`，`append_log/read_log/StageContext`；`backend/app/routers/jobs.py:24–35`；`frontend/src/components/JobPanel.tsx:13–62`；`backend/app/render/train.py:344–357`；`backend/app/services/runs.py:159–184`，`read_log`；`backend/app/pipelines/run.py:300–318`，`_ingest_metrics`；`backend/app/metrics/verl_log.py:16–40`，`parse_chunk`；`backend/app/services/observability.py:101–160,401–439`，`query_session_spans/sample_trace`。

## 4. 准备流程与实际 pipeline

这些是相互依赖的业务分支，不是每个请求依次执行的单一 pipeline。表内名称对应注册的真实 stage。

| Job type | 注册顺序 | 核心作用 / 证据 |
| --- | --- | --- |
| setup.region | identity → bucket → roles → ecr | 区域身份、S3、AgentCore/CodeBuild role、Agent/Trainer ECR；`backend/app/pipelines/setup.py:56–65`，`register` |
| cluster.create | validate → stack → discover → component.* | 新建官方 HyperPod EKS stack，再发现/装组件；`backend/app/pipelines/cluster.py:326–356` |
| cluster.import / cluster.components | discover → component.* / 仅 component.* | 导入及组件修复；`backend/app/pipelines/cluster.py:358–361` |
| agent.build / agent.import | context → checks → build → probe → push / inspect | 构建/导入镜像；`backend/app/pipelines/agent.py:121–141` |
| agent.deploy | deploy → smoke | PUBLIC 或 VPC runtime；可 skip_smoke；`backend/app/pipelines/agent.py:361–392` |
| dataset.ingest / dataset.builtin | ingest / generate | 上传或内置数据转 parquet/S3；`backend/app/pipelines/dataset.py:38–105` |
| trainer.build | source → build | 打包 toolkit 输入并 CodeBuild；`backend/app/pipelines/trainer.py:74–114` |
| run.train | preflight → image → capacity → submit → monitor → finalize | 训练主线；`backend/app/pipelines/run.py:545–555` |
| export.merge | merge | checkpoint 合并与 S3 导出；`backend/app/pipelines/serving.py:40–106` |
| inference.deploy / inference.delete | deploy / delete | vLLM endpoint；`backend/app/pipelines/serving.py:112–183` |
| eval.run | observability → evaluate | 可选 trace 准备、样本评测；`backend/app/pipelines/serving.py:289–317` |

集群 component 的精确顺序为 `access → reach → namespace → irsa → node_ecr → security_groups → kuberay → gpu_plugins → lbc → fsx → guardian`，实际 stage 前缀为 `component.`。新集群初始 CPU system 节点，GPU 由后续扩容取得。区域预检有 STS、权限模拟、配额、本地工具和 toolkit 路径，部分异常只 warning；Settings UI 不要求预检通过才允许 setup，服务端 `require_region` 才是下游区域门槛。

证据：`backend/app/pipelines/cluster.py:184–195,326–361`，`_component/COMPONENT_STAGES`；`backend/app/services/hyperpod.py:51`，`cfn_parameters`；`backend/app/services/project.py:57,127,223,251`，`require_region` 与 setup/preflight helpers；`frontend/src/pages/Settings.tsx:24–35,114`，`runPreflight/runSetup`。

Agent 静态检查主要验证 `AgentCoreRLApp/rollout_entrypoint/_rollout.api_key` 的字符串/AST 契约并警告 V2 模块级随机值/时间。arm64 镜像 probe `/ping`、ECR push 与 runtime READY 不是训练质量验证；导入镜像主要查架构和大小。默认 smoke 用 Bedrock 测是否样本返回成功和数值奖励，不验证 GPU gateway 链路；可跳过 smoke，因此 ready 不等于 smoke 通过。VPC 部署使用私有子网/sg_acr，auto platform 依据源码区域名单；遇支持 AZ 限制时可删失败 runtime 后缩减子网重建一次。

证据：`backend/app/services/agents.py:128–339,425,571`，`static_checks/inspect_image` 及 runtime/smoke helpers；`backend/app/pipelines/agent.py:173–189,361–392`，`stage_deploy/stage_smoke`。

Dataset 支持 JSONL/JSON/CSV/parquet，规范化 payload，可附显式 chat prompt，拒绝 `_rollout/_config` 用户保留字段。GSM8K val 是 test 前 200 条，与完整 test 重叠，不是独立留出集。OfficeBench 组装任务/testbed，经 toolkit 辅助上传并按目录/应用数量分层划分。模板中的 verified 标签只是仓库历史声明；OfficeBench 小模型 FSDP LoRA 配方还显式标注未验证。

证据：`backend/app/services/datasets.py:47,102,155,216`，数据读取/规范化与 GSM8K/OfficeBench helpers；`backend/app/pipelines/dataset.py:77–97`，`stage_builtin`；`templates/gsm8k_math/template.yaml:38`；`templates/officebench/template.yaml:23`。

## 5. 训练规划、参数与完整 Run 时序

### 5.1 从模型/资源输入得到建议

`planner.plan()` 无 I/O，是启发式建议器：auto 遇 MoE、≥20B 参数或上下文 ≥65536 选择 Megatron，否则 FSDP。full 训练状态按约 `16 × 参数量 / world_size` 字节/GPU 粗估，低于半数显存视为舒适；auto 在不舒适或 Megatron 下选 LoRA。LoRA 冻结基座、训练低秩适配器，full 更新全部参数。

TP/CP/EP 分别为张量/上下文/专家并行；FSDP 训练侧均为 1，rollout TP 单独估算。Megatron TP 按权重显存需求取 2 的幂，大模型最低取单节点允许的 TP4；长上下文且有余量选 CP2，MoE EP 取 `min(gpus/TP, 8)`。默认 FSDP LoRA rank32/alpha64，Megatron rank64/alpha128，并输出学习率、offload、vLLM 显存比例与原因/warnings。

**不是保证：**不做真实 GPU 显存模拟、模型收益预测或所有并行整除校验。`plan_overrides` 也没有完整类型/拓扑检查。训练目录限 P 系列，多节点实例能力由 EFA 条件决定；preflight 会硬拒绝不支持的多节点实例。HF chat-template SHA256 匹配 toolkit schema registry 证明兼容路径，不等于该模型训练已验收。

证据：`backend/app/planner.py:63–183`，`plan`；`backend/app/catalog/instances.py:35,105`，`InstanceSpec/spec`；`backend/app/routers/runs.py:79–117`，`_validate`；`backend/app/catalog/models.py:89–97`，模型兼容查询；`backend/app/pipelines/run.py:93–109`，`stage_preflight`。

### 5.2 参数分层与跨层时序

前端只提交显式 overrides，后端 `merge_params` 以 DEFAULT_PARAMS 为基础，忽略 None、拒绝未知字段并做边界检查；spec 保留原始用户 params，render 时再合并。Hydra 渲染以 FIXED 契约和策略分支为基础，结合 planner 与用户参数；agent-loop YAML 再解析模板 loop/default。这里不是任意用户 Hydra 字符串透传，也不是用户能覆盖所有固定契约。

证据：`frontend/src/pages/Runs.tsx:157–204`，`paramDefaults/body/submit`；`backend/app/routers/runs.py:183–191`，`create`；`backend/app/render/train.py:33–55,112–128,139–305`，`FIXED/merge_params/overrides/agent_loop_yaml`；`backend/app/services/runs.py:30–108`，`render`。

1. **UI preview → create**：preview 校验 Runtime/Dataset/模型/参数与计划并估价；用户费用确认后 POST `confirm_cost:true`。后端再次校验并要求 region ready，提交 Run，随后独立提交 Job。
2. **preflight**：检查真实业务前置条件（集群 ready、训练 split、多节点能力等），Run 进入 preparing；这些仍可异步失败，preview 不是云端预留。
3. **image**：按 region/profile/toolkit revision 查找镜像，必要时触发独立 trainer.build 并等待；Agent arm64 镜像不等于 Trainer amd64 镜像。
4. **capacity**：HyperPod 支持 On-Demand/Spot/Training Plan，EC2 node group 支持 On-Demand/Spot。查询现有池，按需扩容/等待；EC2 ready 同时参考可调度 GPU Node 和 ASG InService，不能把将终止节点当可用容量。
5. **submit**：合并参数，生成 Hydra/agent-loop YAML/train.sh/ConfigMap/RayJob；attempt 参与 RayJob 名称，先查同名对象再创建。Ray head 本身占完整 GPU 节点，加 nodes-1 workers；system CPU 仅运行 submitter。driver 拉 S3 数据和 HF 模型到 FSx，然后启动 `verl.trainer.main_ppo`。
6. **monitor → finalize**：轮询 RayJob、读取 S3 日志并更新指标/估算费用；成功读取 checkpoint 索引、释放 RayJob并条件缩容；失败分支见第 7 节。控制台同时轮询业务资源和 Job，不等 HTTP 请求持有到训练结束。

证据：`backend/app/routers/runs.py:79–196`，`_validate/preview/create`；`backend/app/pipelines/run.py:93–297,392–555`，六阶段；`backend/app/pipelines/trainer.py:37–114`，`find_or_create`；`backend/app/services/pools.py:169–223`，池状态；`backend/app/render/train.py:308–375,405–526`，`train_script/rayjob`。

### 5.3 训练面而非 FastAPI webhook

固定配置为 GRPO、`agentcore_sync`、toolkit `PayloadDataset/AgentCoreAgentLoop`，vLLM rollout mode=async **不表示策略更新整体异步**。FSDP LoRA 设置 safetensors load_format；Megatron 选 `ppo_megatron_trainer` 配置，LoRA merge。

应画：Ray/verl → toolkit AgentLoop → AgentCore Agent/工具；Agent 的模型请求回连 rollout gateway `18765` → vLLM。结果与奖励按 toolkit 接口/S3 返回训练侧。FastAPI 负责编排/观测，不接一个假想的 AgentCore 训练结果 webhook。TuningPad 提供 runtime ARN、S3 prefix、端口和 `require_registered_sessions=True`；token/logprob 对齐、session 注册拒绝机制、权重同步及完整失败分类属于外部 toolkit/verl，此轮未审计。

证据：`backend/app/render/train.py:33–55,201–223,276–305,327–357,371`，固定配置与环境；`templates/gsm8k_math/agent/rl_app.py:32–58`，`_rollout` 连接参数、截断转零奖励；`backend/app/services/agents.py:558`，toolkit rollout 接入。

## 6. 前端数据流与反馈闭环

入口 `StrictMode → AuthGate → App`，先加载样式/i18n，认证未决不渲染业务页。App 懒加载页面，Shell 管导航、语言、Toast、Suspense/Outlet；资源子页用 `?view=new|detail&id=…&tab=…`，不是 `/runs/:id`。

名称应对应实际入口：Setup 在 `/settings`，Trainer 在 `/resources` 镜像卡片，Serving 分散在 `/exports`、`/inference`、`/evals`。Clusters 预览后确认创建/导入、维护组件和池；Agents 从 template/ZIP/image 构建/导入后部署；Datasets 上传或内置生成，详情随 ID/split/status 刷新预览；Runs 串起主线；Exports/Inference/Evals 是训练后可选分支。

证据：`frontend/src/main.tsx:12`；`frontend/src/App.tsx:22`，`App`；`frontend/src/v2/Shell.tsx:28`，`Shell`；`frontend/src/pages/Runs.tsx:582`，`RunsPage`；`frontend/src/pages/Settings.tsx:24–35`；`frontend/src/pages/Clusters.tsx:109,253,729`；`frontend/src/pages/Agents.tsx:117,243`；`frontend/src/pages/Datasets.tsx:88,188`；`frontend/src/pages/Resources.tsx:68`；`frontend/src/pages/Exports.tsx:39`；`frontend/src/pages/Inference.tsx:34`。

### 6.1 状态各有所有者

| 状态 | 所有者 | 不应推导的保证 |
| --- | --- | --- |
| draft/step/preview/busy/confirm | 页面组件本地 useState | URL 不持久化未提交表单；busy 不是后端幂等机制 |
| server snapshot | 每个 useLoad 实例 | 非共享缓存、无自动跨页面失效；保留旧数据且 reload 不可 await |
| Job 状态 | useJob 查询/轮询 | 首次读取即终态不触发 onDone；无 stale-response 隔离 |
| 页面身份 | URL 的 view/id/tab | 不保存全部业务状态 |
| Auth/Toast/语言 | 窄职责 Context 与 i18next | 不是通用全局资源 store |

`useLoad` cleanup 阻止过期 effect 写回，但不取消请求。`usePoll` 只建 interval，不立即调用、不串行化异步请求。列表常用 8 秒；Job 2.5 秒；Run 活跃详情 10 秒、非活跃 60 秒；Cluster 5/16 秒。`JobPanel.onDone` 通知业务 owner 重新加载，不能在取得 job_id 后乐观标为 ready。JobLog byte cursor 增量追加；RunLog 另走 Run API、10 秒轮询且保留末尾 40 万字符。

证据：`frontend/src/v2/hooks.ts:38`，`useLoad`；`frontend/src/lib/poll.ts:3–14`，`usePoll`；`frontend/src/lib/useJob.ts:9–35`，`useJob`；`frontend/src/components/JobPanel.tsx:35–116`，`JobLog/JobPanel`；`frontend/src/pages/Runs.tsx:414–452`，`RunLog/RunDetail`；`frontend/src/pages/Clusters.tsx:739`。

### 6.2 RunWizard 默认值与 API 边界

默认数值作为 placeholder 显示，params 只放显式输入/preset；清空写 undefined，`body()` 忽略 undefined/空字符串而保留零；换模板改变继承显示，不覆盖显式值。缺失/加载失败的模板不是无模板，`defaultsReady` 失败阻止继续。服务端再次解析 defaults/模板/plan，前端校验不能代替 backend preflight。一个值得标注而不顺手修复的局限：RunWizard 目录加载固定查询 `us-east-1`，不能等同所选集群区域的真实报价/配额。

所有请求经 `lib/api.ts` 的 request/requestForm，same-origin cookie；ApiError 按稳定 code 翻译，非 auth API 的 401 发事件使 AuthGate 回登录。Job 查询成功但内部失败是另一条 UI 错误展示路径。DTO 只有 TypeScript 断言，无运行时 schema 验证；Field.required 只是星号，Confirm.busy 也只禁确认按钮。

证据：`frontend/src/pages/Runs.tsx:96,146–214`，`NumField/RunWizard`；`frontend/src/lib/api.ts:20–58,670`，`localizedMessage/parseResponse/request/runApi`；`frontend/src/auth/AuthGate.tsx:19`，`refresh`；`frontend/src/v2/ui.tsx:616,675,817,1060`，`Steps/Field/Table/Confirm`。

## 7. 失败恢复、产物与清理

### 7.1 训练监控与产物

monitor 遇 FAILED/STOPPED、deployment Failed 或已命名 RayJob 消失会有限次重提，默认 max_retries=2。只检查最新 attempt 日志中的 `RolloutFailureGuardError`，命中直接失败以免确定性丢弃错误继续烧 GPU；GPU 802/ECC 等特征可触发同池节点替换，替换探测异常不阻塞原重试。预算超限先删除 RayJob 再抛错误；取消在 monitor 捕获时删除 RayJob、保存 stopped 与成本，向上抛 JobCancelled。

完整 checkpoint 位于同一 Run 的 FSx `ckpt` 路径；S3 周期同步的是训练日志与 checkpoint **索引**，只列仍有 actor/ 的 step，不能声称 checkpoint 自动备份到 S3。恢复靠同目录与 verl auto；不是所有外部故障都保证可恢复。

证据：`backend/app/pipelines/run.py:351–457`，`_replace_faulty_nodes/_non_retryable/stage_monitor`；`backend/app/render/train.py:53,344–357`，`FIXED/train_script`；`backend/app/services/runs.py:26–58`，`run_dir/render`。

成功 finalize 先摄取末尾指标/索引/费用，再 `_release_rayjob`（删除异常写日志），`scale_down_after` 为真才尝试缩池。失败 hook 的删除/缩池也受该开关控制且异常被吞；取消的删除主要在 monitor 分支，不可泛化为任意阶段取消都清理一切。`scale_down_if_idle` 仅看同 cluster/group 指定活跃 Run 状态，不扫描所有 Pod、queued run、推理端点；它与 guardian 的忙闲判定不同。删除 RayJob 和 GPU 降为零也不意味着所有云费用归零。

证据：`backend/app/pipelines/run.py:460–542`，`stage_finalize/_release_rayjob/scale_down_if_idle/_finished`。

### 7.2 训练后分支

Export 用 `verl.model_merger` 合并选中 checkpoint 到 FSx 并同步 S3；小模型在 CPU system（CUDA 镜像需 NVIDIA_VISIBLE_DEVICES=void），大模型借 GPU 节点内存，pipeline 无自动扩容阶段。Inference 接受 HF 或 export 来源，创建 vLLM Deployment/Secret/internal NLB，等待 ready；也依赖已有池容量。Evaluation 由 backend RolloutClient → 同集群 AgentCore → vLLM，取指定 split 前 N 条，写逐样本结果 JSONL、奖励/失败统计；不是随机全数据集质量验收。

导出模型 eval 默认继承训练 validation 的单轮 token/temperature，基座比较需显式对齐。可选 observability 使用 `_obs` twin runtime，不给训练 runtime 加 OTEL；启用 Transaction Search 是账户/区域级操作，须独立确认。

证据：`backend/app/services/serving.py:22–103,201–229`，merge/vLLM manifests/`eval_sampling`；`backend/app/pipelines/serving.py:40–183,189–317`，`stage_merge/stage_deploy_endpoint/stage_eval`；`backend/app/pipelines/agent.py:315`；`backend/app/services/observability.py:73`，`enable_transaction_search`。

### 7.3 集群删除不等于停止本地服务

真实 `cluster.delete` stages 为 `workloads → nodegroups → runtimes → cluster`。先删 Services 让 LBC 释放 NLB，再删 namespace；删平台 managed nodegroups；删关联 runtime（含 `_obs`）并在新建集群路径等待 AgentCore ENI 释放；最后处理平台 SG 依赖和 CloudFormation stack，SG 仍未释放时有超时继续/后续重试分支。部分清理异常只写日志，不应承诺全量删除成功。

**复核补充：**导入集群在最终 `stage_delete_cluster` 内跳过 stack 删除、日志写“only unregistering”，但前面的 workloads/nodegroups/runtimes stages 仍注册且运行，不能将整个 delete 描述成“仅从台账注销、绝无云副作用”。这是用户危险操作的源码解释，不是建议在本任务中调用删除 API。

证据：`backend/app/pipelines/cluster.py:576–645,647–769`，`stage_delete_nodegroups/stage_delete_runtimes/stage_delete_workloads/stage_delete_cluster/_delete_platform_sgs/register`。本地 `stop.sh:1–10` 只停止进程组，二者毫无替代关系。

## 8. 安全与费用保护的条件

### 8.1 认证/网络/权限

- **单 operator**：AuthMiddleware 只检查 `/api/`。无密码时仅回环 client 可访问；有密码时除健康/auth 白名单外须有效签名 Cookie。启动拒绝无密码 prod/非回环 host。Cookie HttpOnly、SameSite=Lax，Secure 配置默认 false；密码不等于 HTTPS。未固定 session_secret 时进程随机密钥令重启旧 session 失效。反向代理的地址转发/外部暴露仍须部署审查。
- **网络规则是“添加”不是环境证明**：AgentCore SG → gateway 18765、vLLM 8000，NLB SG → vLLM；inference manifest 使用 internal NLB。代码没有撤销已有宽松规则，不能断言实际账户永无其他入口。远程 CloudFormation 网络定义未审计。
- **IRSA 具体约束**：OIDC trust 限精确 namespace/service account subject 和 sts audience；workload/guardian/LBC 角色分开。workload policy 可访问平台 bucket 和本账户本区域 runtime/*，非每 Run 隔离；部分只读权限/LBC policy 有 `Resource:*`。不宣称“无通配最小权限”。
- **会话契约**：agent_loop_yaml 强制 registered sessions，仅证明本仓库输出配置，不证明未审计 toolkit 的运行时拒绝机制。
- **日志**：没有中央脱敏器；Job 错误 detail、第三方子进程输出、traceback 可进入日志。HTML 只引用源码，不复制真实日志、Cookie、密钥、ARN、账户信息。

证据：`backend/app/core/auth.py:68–103`，`AuthMiddleware/validate_startup`；`backend/app/core/config.py:38–95`，`Settings`；`backend/app/routers/auth.py:24–37`，`login`；`backend/app/services/components.py:114–238,290–339`，`irsa_trust/workload_policy/guardian_policy/ensure_security_groups`；`backend/app/services/serving.py:201–220`，internal NLB manifest；`backend/app/render/train.py:276–305`，`agent_loop_yaml`；`backend/app/jobs/engine.py:368–375`，`_run`；`.trellis/spec/backend/logging-guidelines.md:75–93`。

### 8.2 四个费用层次不能合成一个硬上限

| 层次 | 实现 | 局限与证据 |
| --- | --- | --- |
| 人工确认 | 新 Run confirm_cost；集群/EC2 池目标数量>0 要确认，缩零不用 | Run resume 不再次确认；Training Plan 的 confirm_upfront_fee 只回显未比实时成交价。`backend/app/routers/runs.py:163–196,293–301`；`backend/app/routers/clusters.py:272–348,439–454` |
| 后端 Run budget | monitor 估算当前费用达到阈值，删除 RayJob并失败 | 后端离线该层不工作；不是完整 AWS 账单，准备/等待与所有基础设施费用并非完整计入。`backend/app/pipelines/run.py:321–348,392–448`，`_cost/stage_monitor` |
| RayJob deadline | max_hours 有值才转秒并写 activeDeadlineSeconds | 每 attempt 的 RayJob 限制，不包含整个 Run 的准备/容量等待/所有重试总时长。`backend/app/services/runs.py:91–104`，`render`；`backend/app/render/train.py:525–526`，`rayjob` |
| guardian | 独立集群 CronJob 每五分钟，system CPU 节点；闲置超阈值或预算达到可缩零 | 不是后端 reconciler，也非全账户预算；依赖 K8s/AWS/S3、权限、节点与启动时包获取。`backend/app/services/components.py:622–667`，`guardian_cronjob` |

guardian 读取 tuningpad namespace Pending/Running Pod，按 node/nodeSelector 判断池忙闲；卡住的 Pending 也可一直算忙。第一次闲置观察只启动计时；默认闲置 30 分钟；预算达到可缩忙池。managed=false 不执行缩零，EC2 更新中留待下轮，system 池排除。账本按采样数量×时间×配置单价，未知价可为零，预付 Training Plan 的运行单价也可为零，故不是 AWS 账单。首次采样基线、周期轮询与云缩容都有延迟。

证据：`cluster_assets/guardian.py:57–73,104–182`，`busy_groups/main`；`backend/app/pipelines/cluster.py:264–316`，`guardian_config_for`。成功/失败清理条件参见第 7 节，不能保证即时停费。

## 9. 测试、质量门禁与未验证事项

`backend/tests/conftest.py:16–45,48–84` 的 `_isolated/client/StubClient` 逐测试隔离临时数据目录/SQLite/配置，安装拒绝真实 AWS/K8s 的工厂，TestClient 关闭自动恢复/reconciler。它不是全局网络沙箱，独立 SDK/HTTP/子进程仍须显式 stub；guardian 测试独立替换 boto3/K8s（`backend/tests/test_guardian.py:65–90`）。

| 已读测试源码 / 代表符号 | 覆盖意图（未执行） |
| --- | --- |
| `backend/tests/test_engine.py:22–114`，`test_stages_run_in_order_and_persist_context/test_failure_records_code_and_retry_resumes_from_failed_stage/test_cancel_interrupts_sleep/test_resume_pending_reruns_running_jobs/test_wait_until_timeout/test_log_offsets` | 阶段顺序、context 持久化、跳过成功阶段、协作取消/恢复/超时、日志 cursor |
| `backend/tests/test_planner.py:7`，planner 测试组 | 策略和并行配置决策，不是 GPU 显存/收益验收 |
| `backend/tests/test_runs.py:193–205,319–338,504–575,656–687`，`test_agent_loop_yaml_forces_registered_sessions/test_run_budget_stop/test_rollout_failure_guard_fails_the_run_without_retry/test_scale_down_ignores_runs_on_other_pools` | YAML 安全配置、manifest deadline、成功/重试、预算与 guard、同池清理 |
| `backend/tests/test_guardian.py:103–145`，`test_busy_group_is_left_alone/test_pending_pod_counts_as_busy/test_idle_group_scaled_to_zero_after_threshold/test_first_idle_observation_only_starts_timer/test_budget_scales_even_busy_groups` | 忙闲/首次计时/预算分支 |
| `backend/tests/test_core.py:12–47`；`backend/tests/test_cluster.py:121–193` | 认证、SG 与 IRSA 等边界断言 |

规范门禁 `make verify` → `scripts/verify.sh:12–34`：backend Ruff check/format、pytest -n auto、start.py Ruff/编译与 stop.sh 语法、cluster/trainer/template shell 语法、frontend ESLint、TypeScript + Vite build、i18n key parity。`scripts/i18n_check.py --strict` 才加硬编码文本/静态 key 扫描，仍是启发式。`frontend/package.json:6–34` 无前端 test 脚本/测试框架，构建通过不等于浏览器/E2E 通过。

**当前验证状态：**本节点只做源码/规划阅读和关键分支交叉检查；实施后门禁、浏览器离线/响应式/打印检查及目标 HTML 结构/锚点/链接检查均待批准后的实施。目标 `docs/core-logic.zh-CN.html` 尚未创建，不能报告这些检查通过；其他已有 HTML 不计为本任务交付。

**明确未验证：**AWS/K8s 部署、IAM/SG 实际生效、GPU/镜像/EFA 实际运行、模型收益、价格/账单、并发/取消竞态复现、删除清理效果、toolkit/verl 内部实现、远程模板/供应链。既有代码疑点只作为风险标注，不授权修复。后续 `make verify` 若失败必须记录真实退出码/分节结果，不能为了文档交付修改业务代码或弱化门禁，也不能把“仅既有失败”写成“通过”。

## 10. 阅读地图与 HTML 取材准则

| 想回答的问题 | 优先入口 → 下一跳 |
| --- | --- |
| 一次请求如何成为长任务？ | `backend/app/routers/runs.py:164`，`create` → `backend/app/jobs/engine.py:181,324`，`start/_run` → `backend/app/pipelines/run.py:545`，注册表 |
| 业务对象保存了什么？ | `backend/app/models.py:118,203`，`Run/Job` → `backend/app/core/db.py:44–109`，session/JSON/schema 规则 |
| 为什么选择该并行/微调策略？ | `backend/app/planner.py:63`，`plan` → `backend/app/render/train.py:112,139,276`，参数/Hydra/loop 渲染 → `backend/app/services/runs.py:30`，对象装配 |
| Agent 如何请求训练中的模型？ | `templates/gsm8k_math/agent/rl_app.py:32` → `backend/app/render/train.py:276`，toolkit AgentLoop 配置与第 5.3 节边界 |
| 前端为什么显示旧数据/默认值？ | `frontend/src/v2/hooks.ts:38`，`useLoad` → `frontend/src/lib/useJob.ts:9` → `frontend/src/pages/Runs.tsx:118–214`，`RunWizard` |
| 失败后如何恢复/停费？ | `backend/app/pipelines/run.py:392–542` → `cluster_assets/guardian.py:104` → `backend/app/pipelines/cluster.py:761`，不同清理层 |
| 启动/镜像/模板藏在哪里？ | `start.py:58`，`main`；`stop.sh:1–10`；`trainer_image/Dockerfile:1–45`；`templates/gsm8k_math/template.yaml`；`backend/pyproject.toml:32–35` |
| 有什么测试而非运行保证？ | `backend/tests/conftest.py:16` → `backend/tests/test_engine.py:22`、`backend/tests/test_runs.py:542` → `scripts/verify.sh:12–34` |

术语宜首次解释：控制面/训练面、Job/stage/Run/attempt、台账、幂等、GRPO、FSDP/Megatron、full/LoRA、TP/CP/EP、rollout、gateway、IRSA、EFA、checkpoint、guardian。不要把说明性概念定义写成未查证的 toolkit 算法细节。

HTML 以“事实 → 一张图/表 → 源码位置 → 限制”组织；每节有就近出处，附录是导航而非掩藏限制的位置。相对链接从 `docs/` 用 `../backend/...` 等指向文件，可见标签写 `路径:行号 · 符号`，不加没有本地含义的 `#Lxx`。外部 sibling 路径作为文字说明，不制造不能通过本仓库路径验收的链接。重要区别必须在图中和正文同时成立：

- 控制台 HTTP/轮询、线程池 stage、K8s/云资源与 guardian 分区；reconciler 只标框架无注册。
- Job 与 Run、RayJob 三类名字/状态分开；取消、retry、resume 与 attempt 箭头注明对象。
- AgentCore → gateway 是模型请求回连，不是 FastAPI webhook。
- 完整 checkpoint 是 FSx，S3 是日志/索引和显式 export；训练后部署不暗示自动扩容。
- 设置 deadline、确认成本、scope trust、配置 registered sessions 都是有条件保护，不是完整安全/成本/可用性证明。

无阻塞产品问题；最终 HTML 的实现只缺父代理向用户提交最新规划摘要并取得后续明确批准。
