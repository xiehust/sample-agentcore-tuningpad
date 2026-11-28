# agentcore-rl-toolkit 升级调研：从 xiehust fork 切回 awslabs 上游

调研日期：2026-11-27（UTC）。只读调研，未改代码，未访问 AWS。
上游仓库：<https://github.com/awslabs/agentcore-rl-toolkit>，fork：<https://github.com/xiehust/agentcore-rl-toolkit>。
本地只读克隆：`/tmp/arl-upstream`（awslabs main）、`/tmp/arl-fork`（xiehust main）。

## 1. 摘要

- **结论**：可以切回 awslabs，但不能直接改 URL。上游从 v0.1.5 到 HEAD 共 63 个提交，其中 v0.2.0 是**破坏性**版本。agent 契约（`AgentCoreRLApp` → `AgentCoreRuntimeApp`、`_rollout` → `_config`、`rewards` → `reward`）、verl agent loop 配置（`agent_runtime_arn`/`s3_bucket`/`exp_id` → `rollout_session_backend`）、依赖 extras、verl 0.9.0 → 0.10.0.dev0（git）、vLLM 0.24 → 0.32 都变了。
- **fork 独有的两项关键能力上游都没有**：`require_registered_sessions` 网关鉴权（AGENTS.md 明确列为不能削弱的安全边界），以及 rollout 失败分类加 drop-fraction 熔断（`RolloutFailureGuardMixin`）。上游 `AgentCoreAgentLoop.__init__` 用 `**kwargs` 吞掉未知参数，所以直接切换时这两组 YAML 参数会被**静默忽略**，没有任何报错。
- **建议目标 SHA**：`a187900b345077762b32a6e67b0609989f7de6d8`（awslabs main HEAD，2026-11-25）。它的 CI 检查（test、pre-commit、Core installation 3.11/3.12、SageMaker SDK Tests、CodeQL）全部 success，只有 Dependabot 失败（来源：`https://api.github.com/repos/awslabs/agentcore-rl-toolkit/commits/a187900…/check-runs`）。不建议停在 tag `v0.2.0`（`7ba8960`），因为之后还有 Qwen3.6/3.8 新模板哈希（`7706803`）、history 默认 linear（`0e5c08f`）、网关修复（`15099a4`、`db66760`、`8ae39e6`）、cupy 修复（`aa0afd8`）。
- **切换前置条件（P0）**：先在 TuningPad 里补回网关鉴权（向上游提 PR，或由 TuningPad 在 trainer 镜像中自带补丁），并保留 megatron-bridge CP clamp 补丁，然后再切 URL。

## 2. 版本关系

| 项 | 值 | 来源 |
|---|---|---|
| TuningPad 当前 pin | `xiehust/agentcore-rl-toolkit@7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6` | `backend/pyproject.toml:34` |
| pin 是否在 awslabs 历史中 | **不在**（在 awslabs 克隆中 `git cat-file -t 7712ae7` 报 not found） | `/tmp/arl-upstream` |
| pin 与 awslabs 的 merge-base | `6601bf8251063c4d0197949f08d7356de9fe6d0b` = tag **v0.1.5**（2026-10-03） | `git merge-base` |
| pin 领先 awslabs 的提交（fork 独有） | 7 个（4 个功能/文档提交 + 3 个 merge） | `git log up/main..7712ae7` |
| pin 落后 awslabs main | **63 个提交**（跨 v0.1.6、v0.2.0） | `git log 6601bf8..up/main` |
| awslabs HEAD | `a187900b345077762b32a6e67b0609989f7de6d8`（2026-11-25，feat(examples): add Biomni research agent #242） | `/tmp/arl-upstream` |
| awslabs 最新 release | v0.2.0 = `7ba8960cc967e4a21981e4a4868b01a36328a189`（2026-10-19） | GitHub releases API |
| fork main HEAD | `123e297a369b7246be6aa00bd998d458011e4068`（2026-10-13），比 pin 多合并了 awslabs 到 `b76346c`（v0.1.6 前）的 8 个提交加 1 个测试隔离提交 `99ab5d7` | `git log 7712ae7..origin/main` |

fork 独有提交（在 pin 中）：

| SHA | 内容 |
|---|---|
| `a3f7a14` | feat(gateway): opt-in `require_registered_sessions` guard（未注册 sid 返回 401） |
| `22121e0` | docs: Qwen3.5-2B GSM8K 实验文档 |
| `2788cd3` | feat(verl): rollout 失败分类（model/transient/agent_error/timeout），`RolloutDropped`，`drop_agent_errors`/`max_rollout_retries`/`timeout_policy`，`RolloutFailureGuardMixin` + `agentcore_max_drop_fraction`/`agentcore_drop_guard_steps`，网关 `context_exhausted`/`last_finish_reason`，OfficeBench rl_app 空消息 IndexError 修复 |
| `2f1128a` | docs: 中文核心逻辑指南 |
| `0142ae2`, `642471f`, `7712ae7` | merge |

## 3. 上游变更清单（v0.1.5 `6601bf8` → HEAD `a187900`）

### 3.1 Breaking

| SHA | 变更 | 关键路径 |
|---|---|---|
| `e773d2a` | `refactor(deps)!`：根 `dependencies = []`；新增 extras `server`（bedrock-agentcore）、`rollout`（boto3/aiobotocore/aiohttp/httpx/pydantic/backoff）、`core`=server+rollout；`verl` extra 自带 `rollout,gateway`；移除 `bedrock-agentcore-starter-toolkit`、`python-dotenv` 的传递依赖；包导出改为惰性加载 | `pyproject.toml`、`src/agentcore_rl_toolkit/__init__.py` |
| `f469964`, `3d32b11`, `d825ebc`, `5aa9cbc` | verl `AgentCoreAgentLoop` 改为通过 `make_session` 走 HTTP rollout session：删除 `agent_runtime_arn`/`s3_bucket`/`exp_id` 参数，必须提供 `rollout_session_backend: {backend: agentcore_rl_toolkit.rollout_session.agentcore_http_session.AgentCoreHttpSession, agentcore_runtime_arn: ...}`；新增 `rollout_concurrency`（默认 128，全 job 共享的 Ray actor `rollout_concurrency_semaphore`/`session_create_rate_limiter`，由 trainer 的 `_AgentCoreTrainerBase.init` 创建） | `backends/verl/agent_loop.py:59-147`、`backends/verl/trainer.py:57-83` |
| `d07dc94` | v0.2 契约：`AgentCoreRLApp`、`RolloutClient`、`RolloutFuture`、`AgentCoreS3Session` 标为 deprecated（构造时发出 `DeprecationWarning`）。新 agent 使用 `AgentCoreRuntimeApp` + `@app.entrypoint`，读取 `payload["_config"]`，返回 `{"reward": float}`。旧镜像不能直接用于新 trainer | `MIGRATION.md`、`src/agentcore_rl_toolkit/app.py`、`client.py` |
| `88e71d1` | transformers 5.16.1。`rollout_gateway/response_schemas.py` **删除**，换成 `response_templates.py`（`_TEMPLATE_HASHES` 仍在，并新增公开函数 `resolve_template_name(tokenizer)`）。**移除了 GLM4-MoE 和 Nemotron-3 的模板哈希**。megatron 示例删掉 `vanilla_mbridge`、`grad_offload` 覆盖 | `rollout_gateway/response_templates.py:131-148,203-214` |
| `0e5c08f` | 网关 / agent loop 的 `history_mode` 默认值从 `tree` 改为 `linear` | `backends/verl/agent_loop.py:78`、`gateway_host.py:86` |
| `662f4ff` | 删除 `patches/apply-megatron-bridge-cp-clamp.sh` 和 `patches/megatron-bridge-cp-chunk1-clamp.diff`（以及 verl README 中的 CP 补丁说明） | `patches/` |
| `5057570` | slime backend 从 experimental 移到 `backends/slime` | — |
| `fe325f8` | SageMaker backend 移出 experimental | `backends/sagemaker` |
| `f469964`（agent loop 失败语义） | 有 token 的失败按 0 分训练；没有 trace 的失败直接 raise，sync replay 中只丢掉该 session；reward 缺失或非有限值时 raise。没有 drop-fraction 熔断 | `backends/verl/README.md` Rewards / Trainer observability 节；`agent_loop.py:213-235` |

### 3.2 Feature

| SHA | 变更 |
|---|---|
| `e69295c` | `AgentCoreRuntimeApp(background=True)` 支持不带 envelope 的后台调用；新增 `runtime/{app,client,protocol,store}.py`，提供 `AgentCoreHttpClient`/`InvocationHandle`（start/get 轮询，`stop_runtime_session`） |
| `5e9b035`, `c368149`, `1f8e78f`, `d20826b` | SageMaker training 作为新 backend（含 `train_dapo`） |
| `8ae39e6` | LinearHealer 改为基于 session 树锚定，修复所有分支上的重分词漂移 |
| `d48d50a`, `b97b657`, `ff877ec` | slime：score centering、驱动 `AgentCoreRuntimeApp` agent、升级 slime |
| `4ffee9f` | tinker：限制 rollout 并发 |
| `8862d34` | sandbox：E2B 兼容代理 |
| `357fe4e` | aws_tools：面向 API Gateway 客户端的 SigV4 认证 |
| `31ae88d`, `6e3241d`, `e1cc7df`, `2e59b76`, `3ac1f28`, `f9aa441`, `2e2f40e`, `a187900`, `50cdc96` | 新示例：TMax、terminal agent、SWE-Grep、Strands 生物医学数据库 agent 及 verl 配方、Scale AI AgentEnv、Biomni、TauBench UserSimulator |

### 3.3 Fix

| SHA | 变更 |
|---|---|
| `7706803` | 注册 Qwen3.6/Qwen3.8 带 `{% generation %}` 标签的新模板哈希。提交说明称 hub 上的 `Qwen/Qwen3.6-27B` 于 2026-11-07 修改了 chat template |
| `7c01019` | 新增 Qwen3.8 哈希 |
| `15099a4` | 保留 Qwen3.5 tool-call 参数值中的空白 |
| `db66760` | 格式错误的 Chat Completions `tools` 返回 400（之前不是 400） |
| `aa0afd8` | verl 安装 cupy，供 nccl checkpoint engine 使用（`verl[verl-core,cupy-cu130]`） |
| `bf3c957`, `60744c1`, `aed8ba9`, `64acf8e` | sandbox / TMax 修复 |
| `e2ad5b9` | math agent 示例约束 `strands-agents-tools<0.9.0` |
| `d2c5393` | Strands LiteLLM adapter 的 `num_retries` 恢复为整数 |
| `d07dc94` | OfficeBench `rl_app.py` 改为拼接文本块，不再用 `content[0]` 取值。这与 fork `2788cd3` 修的 IndexError 等价 |

未找到标注为 security 的提交或 advisory。`db66760` 是输入校验加固。

### 3.4 依赖版本（取自 `uv.lock`：pin `7712ae7` vs HEAD `a187900`）

| 包 | pin | HEAD | 备注 |
|---|---|---|---|
| verl | 0.9.0 | 0.10.0.dev0 = git `verl-project/verl@f78fd053e13c3d021e51de5e81afc54dddd13993` | `pyproject.toml [tool.uv.sources]` |
| vllm | 0.24.0 | 0.32.0 | |
| transformers | 5.5.4（lock） / extra 要求 5.12.1 | 5.16.1 | |
| torch | 2.11.0 | 2.13.0 | verl-wheelhouse 索引改为 `cu130/torch2.13` |
| transferqueue | 0.1.8 | 0.1.11 | `01e7e46` |
| megatron-core / megatron-bridge | 0.18.0 / 0.5.0 | 0.19.2 / 0.6.2 | TE 2.16.0 → 2.16.1，`nvidia-modelopt==0.44.0`，新增 `megatron-energon==7.4.1` |
| ray | 2.56.0 | 2.56.0 | 未变 |
| bedrock-agentcore / boto3 | 1.23.1 / 1.43.102 | 1.23.1 / 1.43.102 | 未变 |
| 新增 | — | `uvloop>=0.23.0`（规避 MagicStack/uvloop#738）、`jinja2>=3.1.0` | |
| Python | `>=3.11` | `>=3.11`（megatron 组要求 `>=3.12,<3.13`） | |

## 4. 对 TuningPad 的影响与升级项

优先级：P0 表示切换前必须完成，否则功能或安全会坏；P1 表示切换时应一起做；P2 是可选项。

| # | 变更（上游） | 影响文件:行 | 需要的改动 | 优先级 | 风险 |
|---|---|---|---|---|---|
| 1 | fork `a3f7a14` 的 `require_registered_sessions` 上游不存在；上游 `AgentCoreAgentLoop` 用 `**kwargs` 吞掉未知参数（`agent_loop.py:84`）；上游 adapter 遇到未知 sid 会隐式开 session（`adapters/common.py:384` `self.store.setdefault`） | `backend/app/render/train.py:360-361`；`backend/tests/test_runs.py:201,518,1086`；`AGENTS.md:67`；`README.md:127` | 鉴权必须保留。见第 5 节方案（上游 PR，或 trainer 镜像中自带补丁）。另外加一条运行时断言：渲染出的 loop kwargs 必须被 toolkit 接受（例如 trainer 镜像构建时 `inspect.signature(AgentCoreAgentLoop.__init__)` 中有 `require_registered_sessions`，没有就让构建失败） | P0 | 静默降级为无鉴权；同一 sg-acr 内的任意 runtime 都能打 18765，免费推理并污染轨迹 |
| 2 | verl loop 参数改为 `rollout_session_backend` | `backend/app/render/train.py:351-354`（`agent_runtime_arn`/`s3_bucket`/`exp_id`）、`:445-448`（`ACR_S3_BUCKET`/`EXP_ID` env）；`backend/tests/test_runs.py:196-215` | 渲染 `rollout_session_backend: {backend: agentcore_rl_toolkit.rollout_session.agentcore_http_session.AgentCoreHttpSession, agentcore_runtime_arn: ${oc.env:AGENT_RUNTIME_ARN}}`；删除 `s3_bucket`/`exp_id`；可选择显式设置 `rollout_concurrency` | P0 | 旧 kwargs 在上游会抛 `TypeError`（缺少必需的 `rollout_session_backend`），run 启动即失败 |
| 3 | agent 契约 v0.2（`d07dc94`、`MIGRATION.md`） | `templates/gsm8k_math/agent/rl_app.py:1-4,18,28,32-33,52,58-62`；officebench 的 `rl_app.py` 从 toolkit 复制而来（`templates/officebench/template.yaml:8-9`），上游已迁移 | gsm8k：改用 `AgentCoreRuntimeApp`、`@app.entrypoint`、`payload["_config"]`，返回 `{"reward": float, ...}`；officebench 直接使用上游新文件 | P0 | 旧 agent 镜像与新 trainer 不兼容（MIGRATION.md：「An image built with AgentCoreRLApp cannot be used directly」） |
| 4 | 已部署 agent 与 trainer 契约不匹配 | `backend/app/models.py:62`（`Agent.contract` 默认 `"verl"`）、`:96`（`TrainerImage.toolkit_sha`） | 新增契约版本（例如 `contract="verl-v2"`，或在 Agent 上记录构建时的 toolkit sha）。preflight 拒绝用新 trainer 驱动旧 agent，并给出 `apiErrors.<code>`（中英文 locale 都要加）。遵守 AGENTS.md「不自动更新已有云镜像/runtime」，只提示用户重建 | P0 | 用户选旧 agent 起 run：每个 rollout 都因 `_config` KeyError 失败，GPU 白白消耗 |
| 5 | agent 静态检查和 smoke | `backend/app/services/agents.py:9-10,140-152`（检查 `AgentCoreRLApp`/`rollout_entrypoint`/`_rollout`/`"rewards"`）、`:560-640`（`RolloutClient` smoke，`rewards`） | 静态检查改为 `AgentCoreRuntimeApp`/`@app.entrypoint`/`_config`/`"reward"`。smoke 改用 `AgentCoreHttpClient` + `AgentCoreHttpSession.with_client`（见 `MIGRATION.md` Update evaluation 节） | P0 | `RolloutClient` 走 S3 轮询，不能驱动 `AgentCoreRuntimeApp` agent |
| 6 | 评测 pipeline | `backend/app/pipelines/serving.py:236-247`（`rollout_client(...).run_batch`）；`backend/app/services/evals.py:35,42-48,52-60,299`（`rewards`、`_rollout`、`s3_bucket`、`result_key`、`input_id`）；`backend/app/services/serving.py:3` | 改为异步 `AgentCoreHttpSession` 批量执行，加并发上限和 per-session timeout；把 `reward_of` 改为读 `reward`（过渡期可兼容 `rewards`）；`session_id_of` 改用自己生成的 sid | P0 | 评测全部失败 |
| 7 | 依赖拆分（`e773d2a`） | `backend/pyproject.toml:20`；`templates/gsm8k_math/agent/Dockerfile:7-9`；`templates/officebench/agent/Dockerfile:16-18` | 后端依赖改为 `agentcore-rl-toolkit[rollout]`（smoke/eval 需要 `AgentCoreHttpClient`）。agent 镜像安装 wheel 时带 `[server]`（或 `[core]`），例如 `uv pip install "$(ls /tmp/agentcore_rl_toolkit-*.whl)[server]"`。gsm8k 镜像如用到 `python-dotenv` 要显式安装（officebench Dockerfile 已装） | P0 | `AgentCoreRuntimeApp` 导入失败（缺 `bedrock_agentcore`）；后端 smoke 导入失败 |
| 8 | `response_schemas.py` 被删（`88e71d1`） | `backend/app/catalog/models.py:4,111-115`；`backend/tests/test_catalog.py:160-162,211-214`；`backend/tests/test_toolkit_consumers.py:252-260` | 改为 `from agentcore_rl_toolkit.rollout_gateway.response_templates import _TEMPLATE_HASHES`。更推荐用公开的 `resolve_template_name`（它接收 tokenizer，可传入带 `chat_template` 属性的简单对象）。同步修改测试中的 monkeypatch 目标 | P0 | 模型检查 ImportError |
| 9 | 上游移除 GLM4-MoE、Nemotron-3 模板（`88e71d1`） | `backend/app/catalog/models.py:24`（`glm4moe` parser）、`:38`（`zai-org/GLM-4.5-Air`）、`:172`（错误提示列出 Nemotron-3/GLM4-MoE）；`README.md:20` | 从目录或提示中删掉 GLM-4.5-Air 和 Nemotron-3，或标为不支持；更新 i18n 文案 | P1 | 用户选 GLM 后，网关拒绝带工具的请求（AGENTS.md of upstream：unrecognized template → tool-bearing requests rejected） |
| 10 | Qwen3.6/3.8 新模板哈希（`7706803`） | `backend/app/catalog/models.py:111-115` | 升级后自动获得，无需改代码。**当前 pin 缺少这些哈希**：按提交说明，hub 最新的 Qwen3.6-27B 模板在当前 TuningPad 中会被判为不兼容（未在 HF 上实测，待验证） | P0（升级本身就修复它） | 已验证的 officebench Qwen3.6-27B 预设可能已经无法通过模型检查 |
| 11 | `grad_offload` 不再是 verl 配置键 | `backend/app/render/train.py:314`；`backend/tests/test_runs.py:105`；`backend/app/planner.py:45,150,177`（`grad_offload` 字段） | 删除该覆盖项。依据：用 verl@`f78fd05` 的 `_generated_ppo_trainer.yaml` / `_generated_ppo_megatron_trainer.yaml` 逐一核对 `render/train.py` 中全部 94 个 Hydra 键，**只有** `actor_rollout_ref.actor.megatron.grad_offload` 不存在；`grep -rn grad_offload verl/` 无结果 | P0 | Megatron run 在 Hydra 解析阶段失败（struct 模式下的未知键） |
| 12 | megatron-bridge CP clamp 补丁被删（`662f4ff`），但 **megatron-bridge 0.6.2 并未修复**：wheel 中 `megatron/bridge/models/qwen_vl/modelling_qwen3_vl/utils.py:770-772` 的 chunk one 仍未 clamp | `trainer_image/Dockerfile:38`；`backend/app/render/train.py:402-405`；`backend/app/services/toolkit_source.py:49-50`；`backend/tests/conftest.py:147-148`；`backend/tests/test_toolkit_source.py:58-59`；`backend/tests/test_runs.py:233`；planner 在长上下文时设 `cp=2`（`backend/app/planner.py:141`） | 把补丁移入 TuningPad 自有的 `trainer_image/patches/`，路径改为 megatron-bridge 0.6.2，并核对行号/上下文是否仍匹配。从 `_REQUIRED_FILES` 中移除 `patches/*`，删掉 train script 中的 `|| true` 调用，改为构建期应用 | P0（megatron + CP>1） | 一旦缺失，Qwen3.6-27B（VL 架构）CP=2 遇到短行时，在 `compute_log_prob` 中崩溃（旧补丁的说明）；verl 新版本是否仍合成 len=1 的 padding 行未验证 |
| 13 | fork 的 rollout 失败策略与熔断（`2788cd3`）上游不存在 | `backend/app/render/train.py:366-373`；`backend/app/pipelines/run.py:37-39,424-431`；`backend/app/metrics/verl_log.py:25-31`；`frontend/src/pages/Runs.tsx:488`；`frontend/src/locales/{en,zh-CN}/common.json:119,649`；`backend/tests/test_runs.py:209-215,557-581`；`backend/tests/test_metrics.py:45-58` | 见第 5 节：由 TuningPad 自己实现基于 `training/rollout_failure/total_missing_sessions` 的熔断（上游仍输出该指标，见 `trainer_mixins/variable_row_batching.py:146-150`），或向上游提 PR。删除失效的 kwargs 和指标，更新文案 | P1（建议与切换同批） | 没有熔断时，agent bug 会让 run 一直以大部分 rollout 丢弃或 0 分的状态烧 GPU；`activeDeadlineSeconds` 仍是兜底 |
| 14 | `history_mode` 默认改为 linear（`0e5c08f`） | `templates/gsm8k_math/template.yaml:31-34`（未设置 history_mode）；officebench 已显式设 `linear`（`templates/officebench/template.yaml:20-21`） | gsm8k 显式声明 `history_mode`。要保持已验证行为就写 `tree`；要跟随上游就写 `linear`，并重新验证 | P1 | 已验证的 Qwen3.5-2B GSM8K 配方的行数或指标可能变化（未验证） |
| 15 | trainer 镜像栈升级（verl git/vLLM 0.32/torch 2.13/TE 2.16.1） | `trainer_image/Dockerfile:1-45`（`uv sync --frozen --extra verl` 写法仍可用）；`backend/app/render/train.py:69-72`（Ray 2.56.0 未变，OK） | 无需改命令。需要重建 fsdp 和 megatron 两个 profile 的镜像。镜像按 toolkit sha 作 key（`backend/app/pipelines/trainer.py:38-64`），会自然触发新构建 | P1 | CodeBuild 时长和 wheelhouse 可用性；CUDA 13 驱动要求不变 |
| 16 | vLLM serving 镜像与 trainer 对齐 | `backend/app/services/serving.py:14`（`vllm/vllm-openai:v0.24.0`）；`backend/app/catalog/models.py:20-25`（parser 名） | 升到 `v0.32.0`，并核对 `qwen3_coder`/`qwen3`/`openai`/`openai_gptoss` parser 名在 0.32 中仍然有效（未验证） | P1 | 评测与训练的推理行为不一致 |
| 17 | 测试夹具 | `backend/tests/conftest.py:127-215`（fixture 版本 `0.1.5`、`patches/*`）；`backend/tests/test_toolkit_consumers.py:252-271`（`RolloutClient`） | 同步更新夹具文件清单和版本，更换 monkeypatch 目标。保持 hermetic，用 StubClient 替代 `AgentCoreHttpClient` 的 boto 调用 | P0（`make verify` 必须通过） | — |
| 18 | S3 路径与权限 | `README.md:128`（`rollouts/`、`smoke/` 生命周期）；agent runtime role 的 S3 写权限 | v0.2 rollout 不再写 S3 结果，可收紧 runtime role 的 S3 写权限（最小权限）。officebench 仍要读取任务 S3 | P2 | 收得过紧会影响 officebench 读任务 |
| 19 | 新功能（SageMaker backend、E2B sandbox、新示例 TMax/SWE-Grep/Biomni 等） | `templates/` | 可评估作为新模板 | P2 | — |

IAM 已经覆盖新 HTTP session 需要的动作：`backend/app/services/components.py:148-152` 有 `InvokeAgentRuntime`/`StopRuntimeSession`/`GetAgentRuntime`。上游的 `runtime/client.py:184,191` 会调用 `stop_runtime_session` 和 `invoke_agent_runtime`。

## 5. fork 独有改动及能否切回 awslabs

| fork 改动 | 上游是否有等价实现 | TuningPad 的替代方案 |
|---|---|---|
| `a3f7a14` `require_registered_sessions`（401 拒绝未注册 sid） | **没有**。上游 `rollout_gateway/` 里没有 401 逻辑，`get_or_start_gateway`（`backends/verl/gateway_host.py:77-89`）和 `RolloutGateway.__init__`（`gateway.py:54-65`）都没有这个参数；`adapters/common.py:384` 隐式开 session。上游 issue/PR 搜索 `require_registered_sessions` 结果为 0 | **首选**：把 `a3f7a14` rebase 到上游 HEAD（`ThreadedGatewayServer`/`5aa9cbc` 之后），向 awslabs 提 PR。fork 的改动约 240 行，含 184 行测试，`BaseAdapter.store` 在上游依然存在（`adapters/common.py:163,245`），移植难度低。**过渡方案**：在 TuningPad 的 `trainer_image/patches/` 中维护一个针对 pin SHA 的小 diff，构建时应用，并在镜像构建中断言 `RolloutGateway` 签名含该参数，构建失败即中止。不建议在 trainer 进程内 monkeypatch：网关单例在 `get_or_start_gateway` 内创建后立即开始服务，没有可插入 middleware 的公开钩子。网络层（仅 sg-acr 可达 18765）保留，但它不能替代鉴权 |
| `2788cd3` rollout 失败分类、重试、`RolloutFailureGuardMixin` | **没有**。上游语义：有 token 的失败按 0 分训练；无 trace 的失败 raise 并在 sync 模式下丢弃该 session；只提供 `total_missing_sessions` 指标（`backends/verl/README.md` Trainer observability 节）。搜索 `drop_agent_errors` 结果为 0 | (a) **TuningPad 本地熔断**：`metrics/verl_log.py` 已经解析 `training/rollout_failure/total_missing_sessions`。在 `pipelines/run.py` 的 reconciler 中计算 `missing / (train_batch_size × rollout_n)`，连续 N 步超过阈值就停止 RayJob 并报 `run.rollout_failure_guard`（错误码可保留）。还可参考 `agent_loop/acr_failed/mean`（`AgentLoopMetricsMixin` 从 `extra_fields.metrics.acr_failed` 汇总）。(b) 把 transient 重试和 agent_error 丢弃作为独立 PR 提给上游，上游 README 已承认 async 部分失败组是 future work。(c) 接受上游语义：agent_error 以 0 分参与训练，这会改变学习信号，需要重新验证 |
| `2788cd3` 中 OfficeBench `rl_app.py` 空消息修复 | **有**。上游 `d07dc94` 改为拼接 `"text"` 块（`examples/strands_officebench_agent/rl_app.py`） | 直接使用上游文件 |
| `2788cd3` 网关 `context_exhausted` / `TrajectoryManager.last_finish_reason` | 没有（上游 `src/` 中 grep 不到） | 只服务于 fork 的失败分类；选择方案 (a) 时不需要 |
| `22121e0`、`2f1128a` 文档 | 不需要 | TuningPad 不依赖 |
| fork main 上的 `99ab5d7` 测试隔离（不在 pin 中） | 不需要 | 只影响 toolkit 自身测试 |

**能否切回 awslabs**：能。前提是第 4 节的 P0 项完成，并且网关鉴权通过「上游合入 PR」或「TuningPad 自带补丁」恢复。如果补丁方案也不可接受，就只能继续用 fork，并把 fork rebase 到 awslabs `a187900`。这是第三种选择，但不符合「放弃 fork」的目标。

## 6. 建议的升级步骤

1. **上游 PR（并行进行）**：把 `a3f7a14` 移植到 awslabs main 并提 PR。可选再提一个失败策略 PR。PR 合入后，把下文的目标 SHA 换成合入后的 commit。
2. **pin 切换**：`backend/pyproject.toml:20` 改为 `"agentcore-rl-toolkit[rollout]"`，`:34` 改为
   `agentcore-rl-toolkit = { git = "https://github.com/awslabs/agentcore-rl-toolkit.git", rev = "a187900b345077762b32a6e67b0609989f7de6d8" }`。
   保持 40 位完整 SHA（`toolkit_source.py:108` 校验 `[0-9a-f]{40}`，且只允许 `git`+`rev` 两个键）。之后执行 `cd backend && uv lock && uv sync`。检查 `aiobotocore>=3.9.2` 能否与后端 `boto3==1.43.83` 共存，不行就升 boto3 并保持精确 pin。`toolkit_source` 会在 `<data_dir>/cache/toolkit/<full_sha>` 首次使用时拉取新 SHA，它的 `_github_repo` 身份比对（`toolkit_source.py:119-133`）会随 URL 变化自动适配。
3. **toolkit_source 清单**：从 `toolkit_source.py:37-51` 中删除 `patches/*`；`examples/strands_math_agent/{reward,models}.py` 和 `examples/strands_officebench_agent/*` 在 HEAD 中仍然存在，已用 `ls` 确认。
4. **trainer 镜像**：新增 `trainer_image/patches/`（megatron-bridge 0.6.2 CP clamp + 过渡期网关鉴权补丁），修改 `Dockerfile:38` 的应用路径，增加构建期签名断言；更新 `render/train.py:402-405`。
5. **渲染层**：`render/train.py` 改 `agent_loop_yaml`（`rollout_session_backend`，删除 `s3_bucket`/`exp_id`/失败策略 kwargs），从 `pod_env` 中删除 `ACR_S3_BUCKET`/`EXP_ID`，删除 `grad_offload` 覆盖。
6. **agent 侧**：迁移 gsm8k `rl_app.py`；两个 Dockerfile 的 wheel 安装加 `[server]`；更新 `agents.py` 静态检查和 smoke；引入契约版本，preflight 拒绝旧 agent（新错误码要同时加到 `en` 和 `zh-CN` locale）。
7. **评测**：`pipelines/serving.py`、`services/evals.py` 改为 `AgentCoreHttpSession`，读取 `reward`。
8. **模型目录**：`catalog/models.py` 改用 `response_templates`，删除或标记 GLM4-MoE 和 Nemotron-3，`serving.py:14` 升到 vLLM 0.32.0。
9. **熔断**：在 `pipelines/run.py` 中实现基于 `total_missing_sessions` 的本地熔断，清理 `verl_log.py`、`Runs.tsx:488` 中失效的指标，更新 `asyncHint` 文案。
10. **测试**：更新 `conftest.py` 夹具、`test_runs.py`、`test_catalog.py`、`test_toolkit_consumers.py`、`test_metrics.py`、`test_toolkit_source.py`。所有 AWS/K8s 调用都用 StubClient，保持 hermetic。最后运行 `make verify`。
11. **云端验证（需用户明确授权和成本确认）**：重建 trainer 镜像（fsdp、megatron）和两个模板 agent，跑 GSM8K Qwen3.5-2B 1×H100 的小步数 smoke；确认 guardian CronJob 和 `activeDeadlineSeconds` 仍然生效。这一步按 AGENTS.md 要求，没有用户同意不执行。

## 7. 未确认 / 待验证事项

- 上游 PR 是否会被 awslabs 接受、何时合入：未知。
- Qwen3.6-27B 的 hub 模板是否确实已变、当前 TuningPad 模型检查是否已经失败：只有 `7706803` 的提交说明作为依据，未在 HF 上实测。
- verl@`f78fd05` 是否仍会合成 `prompt_len=1/response_len=1` 的 padding 行（决定 CP clamp 补丁是否仍然必要）：未验证。保守起见保留补丁。旧补丁的 diff 在 megatron-bridge 0.6.2 上能否直接应用：未验证（目标代码行在 `utils.py:770-772`，内容看起来未变）。
- `aiobotocore>=3.9.2`（toolkit `[rollout]`）与 TuningPad `boto3==1.43.83` 能否同时解析：未执行 `uv lock`。
- 后端其他 Hydra 覆盖值（不只是键名）的语义在 verl 0.10.0.dev0 中是否变化，例如 `rollout_is`、`loss_agg_mode`：只核对了键是否存在。
- vLLM 0.32 中 `qwen3_coder`/`glm45`/`openai_gptoss` parser 名是否仍有效：未验证。
- gsm8k 从 tree 改为 linear 后是否影响已验证的指标：未验证。
- `AgentCoreHttpSession` 下 `tps_limit` 的语义（全 job 共享的新会话速率，`trainer.py:66-80`）与旧版（每个 worker 进程各自限速）不同，现有 `tps_limit: 8` 的含义随之改变。是否需要调整：未验证。
- 上游 CI 中 Dependabot 检查失败（与代码无关，原因未调查）。

## 8. 执行结果（2026-11-27）

- 上游 PR：`require_registered_sessions` 移植到 awslabs main 后提交为 [awslabs/agentcore-rl-toolkit#250](https://github.com/awslabs/agentcore-rl-toolkit/pull/250)（commit `86ea8ec`）。
- fork：`xiehust/agentcore-rl-toolkit` main 快进到合并提交 `4fd592c74682f315f412969f975c539c7070ece7`，代码与 `86ea8ec` 相同，只多保留 fork 的两份文档（`experiments/qwen35_2b_gsm8k/`、`docs/core-architecture.zh-CN.html`）。fork 的失败分类/熔断代码不再保留；旧提交（含 `7712ae7`）仍可通过第一父提交访问。
- TuningPad 锁定 `xiehust/agentcore-rl-toolkit@4fd592c`，第 4 节 P0/P1 项已改完：v0.2 agent 契约与契约门禁（`agent.contract_outdated`）、`rollout_session_backend`、HTTP smoke/eval（`services/rollouts.py`）、`response_templates`、删除 `grad_offload`、megatron-bridge 0.6.2 CP clamp 移入 `trainer_image/patches/`、vLLM `v0.32.0`、TuningPad 侧的失败 rollout 熔断（`pipelines/run.py`）。
- 云端：us-east-1 的 gsm8k / officebench Agent 已按 v0.2 重建，两个 PUBLIC smoke Runtime 重新部署，smoke 均 2/2 通过（gsm8k reward 0.0、officebench 1.0，模型 `openai.gpt-oss-20b-1:0`）。Trainer 镜像未重建（无集群，首次创建 Run 时按新 SHA 自动构建）。
- 未验证：GPU 训练端到端；verl 0.10 去掉 `grad_offload` 后大模型显存变化；gsm8k 改用 linear 的影响（当前显式保持 tree）。
