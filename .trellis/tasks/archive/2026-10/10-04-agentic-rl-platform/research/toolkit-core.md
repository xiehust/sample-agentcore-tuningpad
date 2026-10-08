# agentcore-rl-toolkit 核心库与训练后端调研（toolkit-core）

> 源码：`/home/ubuntu/workspace/agentcore-rl-toolkit`（HEAD `0142ae2`，包版本 `0.1.5`）。下文路径默认相对该仓库根。
> 目标：为无代码 Agentic RL 训练平台（tuningpad）确定「用户可调参数」「必需制品」「可复用 AWS helper」「可做成模板的示例」。

## 0. 结论速览

- 训练链路 = **Agent 容器跑在 ACR**（rollout，算 reward，结果写 S3）+ **GPU 训练机**（verl/slime/Tinker 客户端 + 进程内 rollout gateway 做 token 捕获）。两边只通过 ACR runtime ARN、S3 桶、gateway 回调地址（`http://<trainer_ip>:<port>/v1`）三样东西耦合。
- 平台主线建议用 **verl 后端 + `agentcore_sync` 模式 + FSDP**（数学示例已验证；`experiments/qwen35_2b_gsm8k` 有完整的 AWS 端到端脚本：S3/IAM/ECR/ACR/EC2 spot/SG/ckpt 同步，可直接翻译成平台后端逻辑）。Tinker 后端适合「无 GPU 的 CPU 客户端 + 远端 Tinker 端点」模式，可作为第二后端。slime 依赖官方 docker、配置面大，暂不建议首期支持。
- 平台必填参数很少：`AGENT_RUNTIME_ARN`、`ACR_S3_BUCKET`、模型（HF id）、训练/验证 parquet、GPU 数/实例类型、`max_tokens_per_turn`。其余都可给模板默认值。
- `aws_tools` 里的 `agentcore_control.create_agentcore_runtime` **只适用于 A2A + capacity provider（EC2 池）** 的 SWE 场景，**普通 HTTP agent（math/migration 等）不能直接用**；HTTP runtime 应直接调 `bedrock-agentcore-control:create_agent_runtime`（`serverProtocol=HTTP`、`networkMode=PUBLIC|VPC`），或用 starter toolkit 的 `BedrockAgentCoreClient.create_agent`（各示例 `deploy.py` 用法）。`iam_control.ensure_role/ensure_instance_profile`、`dynamodb_control.ensure_session_table` 是通用可复用的。
- 最适合首期模板：**GSM8K Math Agent**（零外部依赖、已验证、单卡可跑）。第二梯队：OfficeBench、Tau-Bench、MigrationBench、AppWorld（需额外数据/依赖/大模型）。SWE（OpenHands）与 Sandbox/Harbor 属于高级场景。

---

## 1. 仓库结构与组件职责

| 路径 | 作用 | 平台相关度 |
|---|---|---|
| `src/agentcore_rl_toolkit/app.py` | `AgentCoreRLApp` + `@app.rollout_entrypoint`：后台跑 agent，结果写 S3 | Agent 镜像必须使用（或用新 `AgentCoreRuntimeApp`） |
| `src/agentcore_rl_toolkit/client.py` | `RolloutClient`/`RolloutFuture`/`run_batch(_async)`：调 ACR、限速、S3 HEAD 轮询、超时自动 `stop_runtime_session` | 平台做「评估 / 冒烟测试」可直接复用 |
| `src/agentcore_rl_toolkit/reward_function.py` | `RewardFunction` ABC（`__call__(**kwargs) -> float \| list[float]`），仅是契约 | reward 模板基类 |
| `src/agentcore_rl_toolkit/rollout_gateway/` | 训练侧 OpenAI/Anthropic 兼容代理：渲染 chat template → token-in/token-out 调采样后端 → 按 session 记录轨迹树 → `TraceRecord` | 训练机内部组件，平台只需暴露端口/鉴权开关 |
| `rollout_gateway/sampling_backends/{vllm_http,sglang_http,tinker_sdk}.py` | 采样后端 | – |
| `rollout_gateway/response_schemas.py` | 按 chat template **sha256 哈希**识别模型族：`qwen3`/`qwen3_5`/`glm4moe`/`gptoss`（含 Qwen2.5、Qwen3(-Instruct-2507/VL/Coder)、Qwen3.5/3.6、Nemotron-3、GLM4-MoE、GPT-OSS） | **平台模型下拉框白名单依据**（未识别模板 → 回退解析，tool-call 可能不可靠） |
| `backends/verl/` | verl 0.9.0 v1 集成：`AgentCoreAgentLoop`、`PayloadDataset`、`agentcore_*` trainer modes、mixins | 主后端 |
| `backends/tinker_api/` | Tinker SDK 训练循环（LoRA + importance_sampling），`python -m agentcore_rl_toolkit.backends.tinker_api.train config.json`；`skyrl/` 为自托管 Tinker 兼容端点（SkyPilot，p4d） | 第二后端 |
| `backends/slime/` | slime（THUDM）集成，`SlimeRunner` + rllm-model-gateway | 暂缓 |
| `backends/experimental/verl/` | `RolloutSessionAgentLoop`（A2A、capacity provider、DynamoDB 会话表），给 SWE 用 | 高级 |
| `rollout_session/` | ACR 会话抽象（HTTP / A2A / S3 / docker A2A），Tinker 和 SWE 用 | – |
| `runtime/` | 新 `AgentCoreRuntimeApp` + `AgentCoreHttpClient`（`_agentcore_runtime` envelope 的 start/get 协议），设计 `designs/agentcore_runtime_app.md` 称其为 `AgentCoreRLApp` 的长期替代 | 注意契约差异（见 §2.3） |
| `sandbox/` + `sandboxd/`(Go) | 任意镜像跑命令：`SandboxClient(runtime_arn).start().exec(argv, cwd, timeout)` | 高级（SWE/Harbor 环境） |
| `aws_tools/` | aioboto3 封装：IAM/ECR PTC/ACR 控制面/DynamoDB/EC2 监控/S3 | 部分可复用（§5） |
| `scripts/build_docker_image_and_push_to_ecr.sh` | `docker buildx --platform linux/arm64 ... --push`，自动建 ECR repo | 镜像构建参考 |
| `experiments/qwen35_2b_gsm8k/` | 真实账号端到端实验（中文文档），含全部 AWS 资源脚本 | **平台后端的最佳参考实现** |

`pyproject.toml` extras（平台安装时按角色选择）：

| extra | 用途 | 关键 pin |
|---|---|---|
| 基础 | agent 侧 | `bedrock-agentcore>=1.23.1`、`bedrock-agentcore-starter-toolkit>=0.2.0`、`boto3>=1.40.55` |
| `rollout` | aws_tools / rollout_session | `aioboto3>=15.5.0`、`httpx`、`pydantic` |
| `gateway` | rollout gateway | `aiohttp`、`transformers>=5.0`、`tokenizers>=0.22.2`、`jmespath` |
| `verl` | 训练机 | `verl==0.9.0`、`vllm==0.24.0`、`transformers==5.12.1`、`torch>=2.10`、`flash-attn==2.8.3`、`transferqueue==0.1.8`（CUDA 13，驱动 ≥ 580.65.06，CC ≥ 7.5） |
| group `verl-megatron` | Megatron 引擎 | Python 3.12，`megatron-core==0.18.0`、`megatron-bridge==0.5.0`、`transformer-engine==2.16.0`、`torch==2.11.0`；VL 模型 + CP>1 需 `./patches/apply-megatron-bridge-cp-clamp.sh` |
| `tinker_api` / `tinker_skyrl` | Tinker 客户端（互斥） | `tinker==0.30.1` / `0.24.1` |
| `slime` | slime | `rllm-model-gateway>=0.1.0` |
| `a2a` / `a2a-server` | A2A 会话 | `a2a-sdk[http-server]>=1.0.1,<2.0` |

安装命令：`uv sync --extra verl`（FSDP）；`uv sync --python 3.12 --extra verl --group verl-megatron`（Megatron）；Tinker：`uv pip install -e '.[rollout,gateway,tinker_api]' 'transformers==5.12.1' datasets wandb`。

---

## 2. Agent 侧契约（平台生成/校验 Agent 镜像的依据）

### 2.1 `AgentCoreRLApp` / `@rollout_entrypoint`（verl、slime、RolloutClient 使用）

- 请求 payload = 数据集 `payload` 列原样 + 训练器注入的 `_rollout`：
  - `exp_id`、`input_id`、`s3_bucket`（三者任一出现即触发 S3 保存，缺一报错）
  - `base_url`（gateway 地址，**含 `/v1`**）、`model_id`（verl 下 = `actor_rollout_ref.model.path`）
  - `api_key`（= 会话 sid = ACR `runtimeSessionId`，gateway 用它区分轨迹；**agent 必须把它放到 LLM client 的 api_key**，否则所有 rollout 落到 "EMPTY" 会话，训练全失败）
  - 可选 `sampling_params`（来自 `RolloutClient(**extra_config)`）
- 入口立即返回 `{"status":"processing","s3_bucket","result_key"}`，后台任务结束后 `put_object` 到 `s3://{s3_bucket}/{exp_id}/{input_id}/{session_id}.json`。
- 返回值必须是 JSON 可序列化 dict；保留键：`status_code`（默认 200，异常时 500）、`stop_reason`、`input_id`、`s3_bucket`、`result_key`、`payload`（**完整 payload 含 `_rollout.api_key` 会被写入 S3**，冒烟时不要放真实凭证）。
- 推荐 pydantic 校验 `prompt: str`（`examples/strands_math_agent/models.py`），防止 toolUse 块绕过模型调用。
- 最小 agent 模板（`examples/strands_math_agent/rl_app.py`）：
  ```python
  app = AgentCoreRLApp()
  @app.rollout_entrypoint
  def invoke_agent(payload, context):
      r = payload["_rollout"]
      model = OpenAIModel(client_args={"api_key": r.get("api_key") or "EMPTY", "base_url": r["base_url"]},
                          model_id=r["model_id"], params=r.get("sampling_params", {}))
      agent = Agent(model=model, tools=[calculator], system_prompt=..., conversation_manager=NullConversationManager())
      text = "".join(b["text"] for b in agent(payload["prompt"]).message.get("content", []) if "text" in b)
      return {"rewards": reward_fn(response_text=text, ground_truth=payload["answer"])}
  ```

### 2.2 Reward 契约（verl：`reward_mode="built_in"` 是唯一支持模式）

- agent 结果里 `rewards`：标量或列表（**取最后一个元素**），直接成为 `rm_scores`。
- 失败 rollout（超时 `max_rollout_time`、ACR 错误、`status_code != 200`）记 0 分；健康 rollout 无 reward → 警告 + 0 分；非数值 → 抛错，该 prompt group 被丢弃。
- 额外指标：agent 返回 `metrics: {name: number}`，在 `agentcore_agent.yaml` 的 `reward_extra_info_defaults: {name: default}` 声明后才会上报；`reward_thresholds: {name: thr}` 生成 `reward_extra_info[name] = 1.0 if reward >= thr`（migration 示例：`task_success: 1.0, build_success: 0.5`）。
- `reward_mode="separate"`（训练端打分）启动即拒绝 → **平台的「reward 函数编辑器」只能把代码打进 agent 镜像**，不能放训练端。

### 2.3 Tinker 后端的契约差异（注意！）

- Tinker 用 `AgentCoreRuntimeApp` + `@app.entrypoint`（`tests/runtime/agents/math/runtime_app.py`），配置字段是 **`_config`**（`base_url`/`model_id`/`api_key`），返回 **`{"reward": float}`（单数、有限标量）**；数据集行不能含 `_config`。
- 即同一个 agent 镜像**不能**同时服务 verl（`_rollout` + `rewards`）与 Tinker（`_config` + `reward`）。平台若同时支持两后端，模板 agent 要兼容两种字段，或按后端分别出镜像。

### 2.4 镜像要求

- ACR 需 **linux/arm64**（`scripts/build_docker_image_and_push_to_ecr.sh`、`experiments/.../agent/build_and_push.sh` 都用 `docker buildx build --platform linux/arm64`），监听 8080（`/ping`、`/invocations`）。
- 基础镜像 `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`；`agentcore configure --entrypoint rl_app.py --requirements-file pyproject.toml --deployment-type container --disable-memory --non-interactive` 可自动生成 Dockerfile（`.bedrock_agentcore/<name>/Dockerfile`，CMD `opentelemetry-instrument python -m rl_app`）。
- 实验踩坑：基础镜像 uv 太旧无法解析仓库 pyproject → 先 `uv build --wheel -o dist/` 再 COPY 安装；去掉 opentelemetry 可缩短冷启动（`experiments/qwen35_2b_gsm8k/agent/Dockerfile`）。平台构建可用 CodeBuild（arm64）替代本地 buildx。

---

## 3. 数据集格式

| 后端 | 格式 | 行结构 | 备注 |
|---|---|---|---|
| verl | parquet（`data.train_files`/`data.val_files` 列表） | `{"payload": {...agent 自己的字段...}}` | `PayloadDataset` 从 `payload[payload_prompt_field]`（默认 `prompt`，`+data.payload_prompt_field=input` 可改）合成 chat 列 `prompt=[{"role":"user","content":...}]` 供 verl 长度过滤；已有 `prompt` 列则不合成（OfficeBench 写了两列）。payload 只能含 JSON 类型；`payload` 外的列保留给调度元数据（如未来的 `agent` 多端点路由） |
| Tinker | JSONL（`dataset`/`evaluation_dataset`） | 每行即 payload：`{"prompt": "...", "answer": "12"}` | 不得含 `_config` |
| slime | JSONL | `{"prompt": "<slime 用于分词/过滤>", "metadata": {...agent payload...}}` | `--input-key prompt` |

GSM8K 生成：`python src/agentcore_rl_toolkit/backends/verl/examples/math_agent/preprocess_gsm8k.py --output-dir gsm8k` → `gsm8k_agent_{train,test}.parquet`（`answer` 取 `#### ` 后、去逗号）。平台「数据集上传」可接受 JSONL/CSV，后台转成单列 `payload` parquet 存 S3，再由训练机 `aws s3 cp` 拉取。

---

## 4. 训练运行的用户可调参数

### 4.1 verl：启动方式

```bash
export HYDRA_FULL_ERROR=1
export VERL_USE_EXTERNAL_MODULES=agentcore_rl_toolkit.backends.verl.trainer   # 注册 agentcore_* 模式；多节点需在每个 ray 节点环境中设置
export AGENT_RUNTIME_ARN=... ACR_S3_BUCKET=...                               # 被 agentcore_agent.yaml ${oc.env:} 引用
python3 -m verl.trainer.main_ppo [--config-name ppo_megatron_trainer] key=value ... "$@"
```
脚本：`backends/verl/examples/math_agent/{fsdp_fft_sync_grpo.sh, fsdp_lora_sync_grpo.sh, megatron_lora_sync_grpo.sh}`、`migration_agent/megatron_lora_sync_grpo_qwen3-coder-30b-a3b.sh`、`office_bench_agent/megatron_lora_sync_grpo_qwen3.6-27b.sh`；单卡实战版 `experiments/qwen35_2b_gsm8k/trainer/train_qwen35_2b.sh`（显式加了 `trainer.use_v1=true`，`AgentCoreAgentLoop.__init__` 强制要求）。

### 4.2 verl Hydra 键（按平台 UI 分层；默认值取自 `fsdp_fft_sync_grpo.sh`，括号内为 2B 单卡实验值）

**A. 固定不暴露（平台写死，否则启动校验失败或语义错误）**

| 键 | 值 | 原因 |
|---|---|---|
| `trainer.use_v1` | `true` | agent loop 返回 list，仅 v1 支持 |
| `actor_rollout_ref.actor.loss_agg_mode` | `seq-mean-token-sum` | `agentcore_*` 模式强制 |
| `data.custom_cls.path` / `.name` | `pkg://agentcore_rl_toolkit.backends.verl.dataset` / `PayloadDataset` | payload 契约 |
| `actor_rollout_ref.rollout.name` / `.mode` | `vllm` / `async` | – |
| `actor_rollout_ref.rollout.calculate_log_probs` | `true` | rollout IS 需要 |
| `actor_rollout_ref.rollout.agent.default_agent_loop` | `agentcore_agent` | – |
| `actor_rollout_ref.rollout.agent.agent_loop_config_path` | 平台生成的 yaml 路径 | – |
| `algorithm.use_kl_in_reward` | `False` | – |
| `trainer.critic_warmup` | `0` | GRPO 无 critic |
| `actor_rollout_ref.model.use_remove_padding` | `True` | 见 token 预算说明 |
| 不要设 vllm `tool_call_parser` | – | 工具调用由 gateway renderer 解析 |

**B. 基础参数（表单直接暴露）**

| 键 | 默认 | 说明 |
|---|---|---|
| `trainer.v1.trainer_mode` | `agentcore_sync` | 可选 `agentcore_colocate_async`、`agentcore_separate_async`（后者要求 `train_batch_size == parameter_sync_step * ppo_mini_batch_size`；colocate 两种要求 `parameter_sync_step=1`） |
| `actor_rollout_ref.model.path` | `Qwen/Qwen3-4B-Instruct-2507`（`/data/hf/Qwen3.5-2B`） | 限 §1 支持的模型族 |
| `data.train_files` / `data.val_files` | `['gsm8k/..._train.parquet']` | Hydra 列表字符串 |
| `data.train_batch_size` | 64（32） | prompt 数/步 |
| `actor_rollout_ref.rollout.n` | 4（8） | GRPO 组大小，每步 rollout 数 = batch×n |
| `actor_rollout_ref.actor.optim.lr` | 5e-6（全参）；LoRA 2e-5；30B LoRA 1e-5 | lr=2e-5 全参且无 KL/TIS 时 ~60 步崩溃 |
| `trainer.total_epochs` / `trainer.total_training_steps` | 1 / （60） | – |
| `trainer.save_freq` / `trainer.test_freq` | 20 / 10 | `test_freq=-1` 关闭验证 |
| `trainer.val_before_train` | true | – |
| `trainer.n_gpus_per_node` / `trainer.nnodes` | 8 / 1（1） | 由实例类型推导 |
| `trainer.project_name` / `trainer.experiment_name` | `agentcore_grpo` / `gsm8k_qwen3_4b` | 也决定默认 `exp_id`（S3 前缀） |
| `trainer.default_local_dir` | `checkpoints/${project}/${experiment}` | 平台再 `aws s3 sync` 到 S3 |
| `trainer.resume_mode` | `disable`（`auto`，spot 续训） | – |
| `trainer.logger` | `["console"]` | `["console","wandb"]` 需 `WANDB_API_KEY` |
| 微调方式（派生） | 全参 | LoRA：`actor_rollout_ref.model.lora_rank=16`、`lora_alpha=32`、`actor_rollout_ref.rollout.load_format=safetensors`；Megatron LoRA：`actor_rollout_ref.model.lora.rank/alpha/merge=True` |

**C. 进阶参数（折叠面板）**

| 组 | 键 | 默认 |
|---|---|---|
| 算法 | `algorithm.adv_estimator` | `grpo` |
| | `algorithm.norm_adv_by_std_in_grpo` | `true` |
| | `algorithm.rollout_correction.rollout_is` / `rollout_is_threshold` | `token` / `2.0`（TIS，稳定性关键） |
| | `actor_rollout_ref.actor.use_kl_loss` / `kl_loss_coef` / `kl_loss_type` | `True` / `0.001` / `low_var_kl`（实验报告建议调大 coef） |
| | `actor_rollout_ref.actor.entropy_coeff` | `0` |
| | `actor_rollout_ref.actor.ppo_mini_batch_size` | = `train_batch_size`（单次更新，KL 为唯一信任域） |
| Token 预算 | `actor_rollout_ref.rollout.max_model_len` | 16384（4096），必须显式设置且 ≤ HF `max_position_embeddings` |
| | `data.max_prompt_length` = `actor_rollout_ref.rollout.prompt_length` | 14336（2048） |
| | `data.max_response_length` = `actor_rollout_ref.rollout.response_length` | = `max_model_len`（gateway 累计轨迹上限，必须 ≤ `max_model_len`） |
| | `actor_rollout_ref.actor.ppo_max_token_len_per_gpu` | = `max_model_len`（8192） |
| | `actor_rollout_ref.actor.use_dynamic_bsz` | `True` |
| 推理 | `actor_rollout_ref.rollout.tensor_model_parallel_size` | 2（1） |
| | `actor_rollout_ref.rollout.gpu_memory_utilization` | 0.5（0.40；30B MoE 0.70） |
| | `actor_rollout_ref.rollout.temperature` | 1.0 |
| | `actor_rollout_ref.rollout.enforce_eager` | False |
| | `actor_rollout_ref.rollout.max_num_batched_tokens` | （Megatron 8192/16384） |
| | `actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu` / `ref.log_prob_micro_batch_size_per_gpu` | 1 / 1 |
| | `actor_rollout_ref.rollout.agent.num_workers` | 1（SWE 64）= gateway 进程数 |
| 验证采样 | `actor_rollout_ref.rollout.val_kwargs.{n,temperature,top_p,top_k,do_sample}` | 1 / 0.6 / – / – / True |
| | `data.val_batch_size` | 256 |
| 显存 | `actor_rollout_ref.model.enable_gradient_checkpointing` | （True） |
| | `actor_rollout_ref.actor.fsdp_config.{param_offload,optimizer_offload}` | False（OOM 兜底） |
| Megatron | `--config-name ppo_megatron_trainer`；`actor_rollout_ref.actor.megatron.{tensor_model_parallel_size,pipeline_model_parallel_size,expert_model_parallel_size,context_parallel_size,sequence_parallel,use_mbridge=True,vanilla_mbridge=False,param_offload,grad_offload,optimizer_offload}`、`...override_transformer_config.recompute_{granularity=full,method=uniform,num_layers=1}`、`actor_rollout_ref.ref.megatron.*`、`...rollout.log_prob_use_dynamic_bsz/log_prob_max_token_len_per_gpu`、`++actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_cpu_offload=True`、`+actor_rollout_ref.rollout.engine_kwargs.vllm.moe_backend=triton`、`actor_rollout_ref.actor.checkpoint.save_contents='["model"]'` | 见 migration 脚本 |

任何 Hydra 键都可经 `"$@"` 追加 → 平台可提供「高级覆盖（key=value 文本框）」。

### 4.3 `agentcore_agent.yaml`（`AgentCoreAgentLoop.__init__` kwargs，**不能**用 Hydra CLI 覆盖，平台需渲染此文件）

```yaml
- name: agentcore_agent
  _target_: agentcore_rl_toolkit.backends.verl.agent_loop.AgentCoreAgentLoop
  agent_runtime_arn: ${oc.env:AGENT_RUNTIME_ARN}   # 必填
  s3_bucket: ${oc.env:ACR_S3_BUCKET}               # 必填
  max_tokens_per_turn: 2048                         # 必填；gateway 单次调用 max_new_tokens（≤ response_length）
  tps_limit: 4                                      # 默认 5；ACR InvokeAgentRuntime 速率（账号新会话 ~25/s）
  max_rollout_time: 180                             # 默认 1800s；超时记 0 分
  # exp_id: <默认由 project/experiment 派生，建议平台固定为 run id>
  # max_pool_connections: 100
  # gateway_bind_host: 0.0.0.0
  # gateway_port: 0                 # 0=自动分配；平台应固定（实验 18765，SG 只开一个端口），用 ${oc.decode:${oc.env:GATEWAY_PORT}} 转 int
  # gateway_public_host: <ip>       # ACR PUBLIC 模式必须设为训练机公网 IP（IMDSv2 public-ipv4）；VPC 模式默认 Ray node IP
  # gateway_adapters: [openai]      # 可加 anthropic
  # require_registered_sessions: false  # 端口暴露公网时必须 true（拒绝未注册 sid，401）
  # history_mode: tree              # tree | linear（多轮追加式 agent 用 linear，migration/officebench）
  # linear_on_nonlinear: reset
  # max_turns_per_sid: null
  # reward_mode: built_in           # 唯一可用
  # reward_extra_info_defaults: {}  # {metric_name: default}
  # reward_thresholds: {}           # {name: threshold}
```

### 4.4 Tinker 后端（`backends/tinker_api/train.py` 的 `Config` dataclass，JSON 文件传入）

| 字段 | 默认 | 说明 |
|---|---|---|
| `endpoint`* | – | Tinker 服务 URL（官方 `https://tinker.thinkingmachines.dev/services/tinker-prod`，或 SkyRL `http://<gpu_ip>:18080`）；`TINKER_API_KEY` 环境变量（空则 `tml-dummy`） |
| `base_model`* / `tokenizer`* | – | 端点模型 id / HF tokenizer id；`tokenizer_revision` |
| `dataset`* / `evaluation_dataset` | – / None | JSONL |
| `agent_runtime_arn`* | – | – |
| `gateway_host`* / `gateway_port` | – / 0 | ACR 回调地址 |
| `output_dir`* | – | 本地输出（`metrics.jsonl`、`rollouts-XXXX.json`、`checkpoint.json`） |
| `chat_template_kwargs` | `{}` | 如 `{"enable_thinking": false}` |
| `history_mode` | `tree` | 示例用 `linear` |
| `steps` / `batch_size` / `group_size` | 3 / 4 / 4 | group_size ≥ 2 |
| `lora_rank` / `learning_rate` | 32 / 1e-5 | 只支持 LoRA |
| `max_new_tokens` / `max_context_tokens` | 2048 / 16384 | – |
| `rollout_timeout` / `tps_limit` | 180 / 4 | – |
| `evaluation_batch_size` / `evaluation_temperature` / `evaluation_interval` / `checkpoint_interval` | 256 / 0.6 / 10 / 20 | – |
| `wandb_project` / `wandb_entity` | None | – |
| `exp_id` | `tinker-<uuid12>` | – |

CLI：`python -m agentcore_rl_toolkit.backends.tinker_api.train config.json [--log-level DEBUG]`。算法固定：组内中心化 reward（无 std 归一、无 clip），`importance_sampling` loss，无方差组跳过。

### 4.5 slime（`SlimeRunner` dataclass，`backends/slime/runner.py`，暂缓）

必填 `exp_id, agent_runtime_arn, s3_bucket, model_dir, data_path, model_type`（如 `qwen3-4B`，对应 `${SLIME_DIR}/scripts/models/*.sh`）；集群 `num_gpus=8, tp_size=2, rollout_gpus_per_engine=2, slime_dir=/root/slime, megatron_dir=/root/Megatron-LM, cuda_home`；ACR `model_id, acr_timeout=900, acr_tps_limit=25, max_concurrent=100, max_pool_connections, gateway_port=9090, reward_postprocessing=grpo|identity, sglang_tool_call_parser=qwen, sglang_reasoning_parser, cumulative_token_mode, renderer_family`；超参 `rollout_batch_size=32, n_samples_per_prompt=8, rollout_max_response_len=1024, rollout_temperature=1.0, lr=1e-6, eps_clip=0.2, eps_clip_high=0.28, weight_decay=0.1, adam_beta2=0.98, sglang_mem_fraction_static=0.7, sglang_context_length, max_tokens_per_gpu=9216`；`wandb_project/group`；`extra_flags`。依赖官方 `slimerl/slime` docker、CUDA ≥ 12.9。

---

## 5. 所需 AWS 制品与权限

| 制品 | 创建方式（参考脚本） | 关键参数 |
|---|---|---|
| ECR 仓库 + arm64 agent 镜像 | `scripts/build_docker_image_and_push_to_ecr.sh --dockerfile=... --tag=... --context=...`（读 `.env`：`AWS_REGION/AWS_ACCOUNT/ECR_REPO_NAME`）；`experiments/.../agent/build_and_push.sh` | `docker buildx --platform linux/arm64 --push` |
| S3 结果桶 | `experiments/.../agent/create_resources.sh` | 公共访问全封；lifecycle：`{exp_id}/` 14 天过期，`smoke/` 1 天；ckpt 另放 `ckpt/` 前缀长期保留 |
| ACR 执行角色 | 同上 | trust：`bedrock-agentcore.amazonaws.com` + `aws:SourceAccount` + `aws:SourceArn` `arn:aws:bedrock-agentcore:<r>:<acct>:*`；权限：ECR `BatchGetImage/GetDownloadUrlForLayer`(+`GetAuthorizationToken`)、Logs、`cloudwatch:PutMetricData`(ns=bedrock-agentcore)、X-Ray、`bedrock-agentcore:GetWorkloadAccessToken*`、S3 `PutObject/GetObject/ListBucket`；agent 用 Bedrock（如 tau-bench 用户模拟器）还需 `bedrock:InvokeModel` |
| ACR Runtime（HTTP） | `experiments/.../agent/create_runtime.sh`：`aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name N --agent-runtime-artifact containerConfiguration={containerUri=...} --role-arn R --network-configuration networkMode=PUBLIC --protocol-configuration serverProtocol=HTTP`，轮询 `get-agent-runtime` 至 `READY`；或 `BedrockAgentCoreClient(region).create_agent(agent_name, deployment_type="container", image_uri, execution_role_arn, network_config, env_vars, auto_update_on_conflict=True)` + `wait_for_agent_endpoint_ready(agent_id, max_wait=120)`（`examples/strands_*_agent/deploy.py`） | VPC 模式：`{"networkMode":"VPC","networkModeConfig":{"subnets":[...],"securityGroups":[...]}}` |
| 训练机 IAM 实例角色 | `experiments/.../infra/create_trainer_iam.sh` | S3 `ListBucket` + 对象 `Get/Put/Delete`；`bedrock-agentcore:InvokeAgentRuntime/StopRuntimeSession/GetAgentRuntime`（`runtime/*`）；`ec2:Describe*`；SSM（远程执行） |
| 训练机 SG | `infra/create_sg.sh` | 入站仅开 gateway 端口（实验 18765）；PUBLIC 模式源只能是 0.0.0.0/0 → 必须 `require_registered_sessions: true`；VPC 模式可限定 ACR 子网 |
| GPU 训练 EC2 | `infra/launch_spot.sh`（spot one-time + 独立 EBS 数据卷 `/data` 300GB + user_data + watchdog/GPU 小时账本）、`resume.sh`、`terminate.sh`、`ssm_run.sh`、`trainer/setup_trainer.sh`、`trainer/sync_ckpt.sh`（`aws s3 sync` ckpt/log） | AMI `Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 24.04)*`；p5.4xlarge(1×H100)/p5.48xlarge(8×H100)；spot 配额 `L-7212CCBC` |
| 网络连通 | – | **ACR 容器必须能访问训练机 gateway 端口**（PUBLIC 模式用公网 IP；VPC 模式同 VPC/路由） |
| （SWE 专用）capacity provider、DynamoDB 会话表、ECR pull-through cache + Secrets Manager Docker Hub token | `examples/openhands_swe_agent/deploy.py` + `aws_tools` | 见 §6 |

实验参考成本（`experiments/qwen35_2b_gsm8k/REPORT.md`）：8×H100 spot，32×8=256 rollout/步，≈101 s/步，60 步 1h41m；val 0.545→0.85（step10）→0.795；ACR 失败率 ~5.5%；GPU ≈ $190（含 6.8h 浪费），单卡方案预计 ~$20。

---

## 6. `aws_tools` 可复用 helper（全部 `async`，需 `[rollout]` extra，aioboto3）

| 模块 | 签名 | 平台可复用性 |
|---|---|---|
| `boto3_tools` | `get_boto3_session() -> boto3.Session`；`async get_aioboto3_session()`；`get_role_credentials(role_arn) -> dict`；`class LongLivedCredentials`；`tags_to_map(tags)`；`class SigV4HttpxAuth(credentials, region, service)`；`async shared_sigv4_httpx_client(region, service)` | ✅ 通用 |
| `iam_control` | `current_account_id(region_name=None) -> str`；`find_role(role_name, region_name=None)`；`ensure_role(role_name, trust_policy: dict, policies: dict[str, dict]=None, managed_policy_arns=(), description="", region_name=None) -> str`（声明式：内联策略全量覆盖）；`find_instance_profile(...)`；`ensure_instance_profile(profile_name, role_name, region_name=None) -> str` | ✅ 直接用于 ACR 执行角色 / 训练机实例角色 |
| `s3_tools` | `upload_object(s3_uri, region_name, data: bytes)` | ✅（仅上传；建桶需自写） |
| `agentcore_control` | `ensure_capacity_provider_roles(region) -> (operator_role_arn, instance_profile_arn)`（固定名 `AmazonBedrockAgentCoreCapacityProviderDefault{Operator,Instance}Role` + AWS 托管策略）；`find_capacity_provider(region, name)`；`create_capacity_provider(region, name, operator_role_arn, instance_profile_arn, subnets, security_groups, instance_type="c5.large", root_throughput=600, ssh_key_name=None) -> arn`；`ensure_capacity_provider(region, name, **kw)`；`find_agent_runtime(region, name)`；`create_agentcore_runtime(runtime_name, region_name, image_uri, role_arn, capacity_provider_arn)`；`update_agentcore_runtime(runtime_arn, image_uri, role_arn, capacity_provider_arn)`；`cleanup_agentcore_runtime(runtime_arn) -> live_version`（删旧版本，版本数有配额）；`ensure_agentcore_runtime(...) -> arn` | ⚠️ runtime 创建写死 `serverProtocol=A2A` + `capacityProviderConfiguration` + lifecycle 3600s，**仅适用于 SWE/A2A**。`find_agent_runtime`、`cleanup_agentcore_runtime`、`_wait_ready` 模式可借鉴；HTTP runtime 需平台自写（参照 `create_runtime.sh`） |
| `agentcore_tools`（数据面） | `region_of(arn)`；`agentcore_client(region, config)`；`shared_agentcore_client(region)`；`start_agentcore_session(runtime_arn, session_id, client=None)`；`invoke_agentcore_session(runtime_arn, session_id, payload: bytes, client=None) -> HttpResponse(status_code, body)`；`stop_agentcore_microvm_session(runtime_arn, session_id, client=None) -> bool`；`stop_agentcore_instance_session(capacity_provider_arn, session_id, client=None)`；`agentcore_session(cp_arn, runtime_arn, session_id)`(ctx mgr)；`is_runtime_readiness_error(e)` | ✅ 平台「Agent 冒烟测试」按钮可用 |
| `ecr_control` | `find_secret(name, region)`；`ensure_registry_secret(secret_name, region, username=None, access_token=None) -> arn`；`find_pull_through_cache_rule(prefix, region)`；`validate_pull_through_cache_rule(prefix, region)`；`ensure_docker_hub_cache_rule(prefix, region, credential_arn) -> str` | ⚠️ 只管 Docker Hub pull-through cache，**不建普通 ECR repo** |
| `dynamodb_control` / `dynamodb_tools` | `ensure_session_table(table_name, region) -> arn`（含 GSI `experiment_sessions`）；`update_dict(...)`、`paginate_table(...)`、`get_sessions_for_experiment_run(experiment_name, experiment_start_at="", *, table_name, region_name=None)`、`get_session(session_id, *, table_name, region_name=None)`；`to_dynamodb/from_dynamodb` | ✅ 若平台想要 rollout 级别看板，可复用会话表模型 |
| `ec2_tools` / `ec2_monitor` | `get_current_instance_type(default="unknown")`；`find_running_instances(capacity_provider_arn, session_prefix=None)`；`start_ec2_monitor(capacity_provider_arn, dynamodb_table, poll_interval, stats_interval, session_prefix=None, region_name="us-west-2") -> EC2Monitor` | ⚠️ 面向 capacity provider，**无启动 GPU 训练机的 helper** → 训练 EC2 编排需平台自写（参照 `launch_spot.sh`） |
| `persistent_dict` | `PersistentDict(data, persister=DynamoDBPersister(table, key, region)|NullPersister())`、`measure_span_persistent(span_name, metrics)` | 可选 |

评估/冒烟用 `RolloutClient(agent_runtime_arn, s3_bucket, exp_id, max_retry_attempts=5, tps_limit=25, max_pool_connections=10, base_url=None, model_id=None, **extra_config)`：`invoke(payload, session_id=None, input_id=None, **rollout_overrides) -> RolloutFuture`、`run_batch(payloads, max_concurrent_sessions, timeout=900.0, initial_interval=0.5, max_interval=30.0, backoff_factor=1.5, log_interval=30.0)`（及 `_async` 版本）。注意 `api_key` 必须作为 `invoke(..., api_key=...)` 传，不能放 payload 的 `_rollout`（会被覆盖）。

---

## 7. 示例 → 无代码模板候选

| 示例（agent 目录 / verl recipe） | 任务 | 数据集 | Reward | 依赖/部署 | 已验证训练配置 | 模板优先级 |
|---|---|---|---|---|---|---|
| `examples/strands_math_agent` / `backends/verl/examples/math_agent` | GSM8K 计算器工具 agent | HF `openai/gsm8k` → `preprocess_gsm8k.py` | `GSM8KReward`：`#### <num>` 严格匹配 → 1/0 | 无外部依赖；`.bedrock_agentcore/strands_math_agent_rl/Dockerfile`；无 deploy.py（用 `agentcore configure/deploy` 或实验脚本） | Qwen3-4B 8×GPU FSDP 全参 ~0.93；LoRA（8×A100 40G）；Megatron LoRA；Qwen3.5-2B 单/8 卡；Tinker Qwen3.5-4B；slime | **P0（默认模板）** |
| `examples/strands_officebench_agent` / `verl/examples/office_bench_agent` | 300 个办公自动化任务（日历/邮件/Excel/Word/PDF/OCR，20 个 tool） | OfficeBench → `preprocess.py` + `preprocess_officebench.py`（payload: `task_uri`,`testbed_uri`，带显式 prompt 列） | 任务 evaluation 配置比对（安全 AST 评估器） | 需 S3 任务数据；无 deploy.py（有 `config.example.toml`） | Megatron LoRA Qwen3.6-27B，`history_mode: linear`，`max_tokens_per_turn: 8192`，1800s | P1 |
| `examples/strands_taubench_agent` | tau2-bench 客服（airline/retail/telecom），Bedrock Claude 做用户模拟器 | `tasks/*.json`，payload 带 `_task` | tau-bench `reward_basis`（DB/COMMUNICATE/ENV_ASSERTION/ACTION） | 执行角色需 Bedrock 权限；无 verl recipe | – | P1（需补 recipe） |
| `examples/strands_migration_agent` / `verl/examples/migration_agent` | Java 8→17 迁移（MigrationBench） | `preprocess.py --s3-bucket-name` 上传 repo tarball + `preprocess_migrationbench.py`（payload: `prompt`,`repo_uri`,`metadata_uri`,…） | 构建成功 + 测试/迁移程度（0/0.5/1） | 镜像含 Java17/Maven；`deploy.py`（starter toolkit，支持 VPC）；注意 config.example.toml 缺 deploy 字段 | Megatron LoRA Qwen3-Coder-30B-A3B（TP/EP），n=16，`reward_thresholds` | P2 |
| `examples/strands_appworld_agent` | AppWorld（Spotify/Venmo/Gmail 模拟 API，Python REPL ReAct） | `appworld download data`，payload `task_id` | 测试通过率 `pass_count/num_tests` | git-lfs、AppWorld 数据打进镜像；`deploy.py` + `config.example.toml`（VPC） | 无 verl recipe | P2 |
| `examples/openhands_swe_agent` / `verl/examples/swe_agent` | SWE-Gym / SWE-bench（OpenHands harness） | SWE-Gym parquet（`preprocess.py`） | 测试通过 | A2A + capacity provider（c5.large 池）、DynamoDB、ECR PTC + Docker Hub PAT、VPC；`deploy.py` 全自动 | Qwen3-Coder-30B-A3B Megatron LoRA，separate-async，Hydra 组合配置 `config/main.yaml` + 自定义 `main.py` | P3（高级） |
| `examples/sandbox_quickstart`、`examples/sandbox_harbor` | 任意镜像沙箱命令执行 / Harbor 基准评估 | – | – | sandboxd Go 二进制 | 仅评估 | 非训练模板，可作「环境」能力 |

模板化要点：每个模板 = {agent 源码目录 + Dockerfile, 数据预处理脚本/默认数据集, reward 说明, `agentcore_agent.yaml` 默认值, verl 参数预设（按 GPU 规格分档）}。用户可编辑项：system prompt、reward 参数（如 GSM8K `method=strict|flexible`、`format_score`）、数据集（同 schema 上传）、模型、超参。

---

## 8. 平台落地注意事项（来自代码与实验踩坑）

1. **网络是第一风险**：ACR → 训练机 gateway 回调。PUBLIC 模式要 `gateway_public_host=<公网IP>` + 固定 `gateway_port` + SG 开端口 + `require_registered_sessions: true`；VPC 模式需 ACR 子网路由到训练机（实验的私网改造未完成，见 `infra/create_vpc_private*.sh`）。
2. `${oc.env:VAR}` 未设置会在 worker init 报错 → 平台启动前统一导出 `AGENT_RUNTIME_ARN/ACR_S3_BUCKET/EXP_ID/GATEWAY_PORT/GATEWAY_PUBLIC_HOST`。
3. 训练机环境：DLAMI 驱动 ≥ 580.65.06；`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0`、直接用 venv python；PATH 加 `.venv/bin` 与 `/usr/local/cuda-13.0/bin`（flashinfer JIT 需 ninja/nvcc）；`HF_HOME` 指向数据卷。建议平台预制 AMI 或训练容器镜像，避免每次 `uv sync`。
4. 成本护栏：spot + 独立 EBS + `trainer.resume_mode=auto` + 周期 `aws s3 sync` ckpt + watchdog GPU 小时上限；启动计费资源前先验证 terminate/SSM 通道可用。verl 本地只保留最近 ckpt，S3 同步不加 `--delete`。
5. 训练监控指标（可直接做平台曲线）：`critic/score/mean`、val reward、`actor/kl_loss`、`response_length/mean`、`batching/total_{real_rows,rows,padding_rows}`、`training/rollout_failure/total_missing_sessions`、`agent_loop/<name>/{mean,min,max,sum}`、`critic/advantages/zero_mean`。console logger 需平台解析日志，或启用 wandb / 自定义 tracker。
6. 速率：`tps_limit` 受账号 ACR InvokeAgentRuntime / 新会话配额约束（实验按 25/s 的 1/3 取 8）。
7. 设计文档动向（`designs/`）：`AgentCoreRuntimeApp`（Accepted, In progress）将替代 `AgentCoreRLApp`；`runtime_invocation_protocol`（Proposed）；`gateway_linear`、`verl_variable_trajectory_batching`（Shipped）；`sandbox_sdk`（Accepted）；`sandbox_dynamic_environments`（Proposed, 未开始）。平台的 agent 模板层应对 `_rollout`/`_config` 两套契约做抽象，以便后续迁移。
