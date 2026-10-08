# AWS 实验工作流调研：qwen35_2b_gsm8k → 平台可自动化的状态机

来源：`/home/ubuntu/workspace/agentcore-rl-toolkit/experiments/qwen35_2b_gsm8k/`（下文简称 `EXP/`）全部脚本与文档（`agent/*`、`infra/*`、`trainer/*`、`research/*`、`*/SPEC.md`、`PLAN.md`、`REPORT.md`、`COST.md`、`env.sh`），外加只读核对的 S3 实际产物与 toolkit 源码（`src/agentcore_rl_toolkit/{app.py,client.py,backends/verl/agent_loop.py,backends/verl/trainer_mixins/variable_row_batching.py}`）。

## 0. 本环境 AWS 凭证核验（只读，2026-10-04）

| 命令 | 结果 |
|---|---|
| `aws sts get-caller-identity` | ✅ 成功。Account `4344****5045`，`arn:aws:sts::4344****5045:assumed-role/admin_role_for_workshop/i-0ccbd99d596e8b744`（EC2 实例角色，非静态密钥） |
| `aws bedrock-agentcore-control list-agent-runtimes --region us-west-2 --max-results 10` | ✅ 成功，返回 10 个 READY runtime（`v2_byoc_zip_demo_0926_872db7`、`srpool_runtime`、`shopagent` …，分页）。用 `--query "agentRuntimes[?starts_with(agentRuntimeName,'qwen35')]"` 查到实验 runtime `qwen35_2b_gsm8k_math_agent-057YOI31DY`，**READY** |
| `aws s3 ls \| grep agentcore-rl` | ✅ `agentcore-rl-qwen35-2b-gsm8k-4344****5045-usw2`（创建于 2026-09-23） |

额外只读核对（桶内容，平台可直接复用的"真实样例"）：
- 顶层前缀：`ckpt/ code/ ledger/ logs/ qwen35-2b-gsm8k/ smoke/`
- `ledger/gpu_hours.json`：`cumulative_hours=9.3544`，2 个 session（均 us-east-2c）
- `ckpt/qwen35_2b_gsm8k/qwen35_2b_grpo/global_step_{10..60}/` + `latest_checkpointed_iteration.txt`
- `logs/`：`setup.log`、`train_smoke.log`、`train_full.log`(536 KB)、`sync_ckpt.log`、`vllm_sanity.log`、`train.pid`
- `code/repo.tar.gz`(2.7 MB)、`code/infra/{watchdog.sh,spot_interrupt_watch.sh}`
- `qwen35-2b-gsm8k/<input_id>/<session_id>.json`：rollout 结果（14 天生命周期过期）

> 未调用 `get-agent-runtime`（research 记录 admin 用户曾缺 `GetAgentRuntime` 权限；当前角色未验证）。未做任何写操作。

---

## 1. 资源命名约定（`EXP/env.sh`，平台应参数化）

| 变量 | 实验值 | 平台化建议 |
|---|---|---|
| `AWS_REGION` | `us-west-2`（ACR/ECR/S3 所在区） | 项目级 |
| `TRAINER_REGION` | 默认 us-west-2，实际跑在 **us-east-2**（`infra/target.env` 持久化） | 每次 run 可变，按 spot 容量选 |
| `EXP_TAG` / `EXP_ID` | `qwen35-2b-gsm8k` | `Project` tag 值 + RolloutClient `exp_id`（S3 前缀）→ 平台用 `run-<uuid>` |
| `ECR_REPO_NAME:IMAGE_TAG` | `agentcore-rl-math-agent:qwen35` | 每个 agent 一个 repo/tag |
| `ACR_S3_BUCKET` | `agentcore-rl-${EXP_TAG}-${ACCOUNT}-usw2` | 平台一个共享桶 + 按 run 前缀，或每项目一桶 |
| `ACR_ROLE_NAME` | `AgentCoreRL-MathAgent-RuntimeRole` | |
| `ACR_RUNTIME_NAME` | `qwen35_2b_gsm8k_math_agent`（只允许 `[a-zA-Z0-9_]`） | |
| `TRAINER_ROLE_NAME` / `TRAINER_SG_NAME` | `AgentCoreRL-Trainer-InstanceRole` / `agentcore-rl-trainer-sg` | |
| `GATEWAY_PORT` | `18765` | 固定 |
| `TRAINER_INSTANCE_TYPE` | `p5.4xlarge`（回退 `p5.48xlarge`/`p5en.48xlarge` 时自动 `N_GPUS=8 TP_SIZE=2`，小时上限收紧为 9/11/12） | |
| `TRAINER_AMI_NAME` | `Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 24.04)*`（us-west-2 固定 `ami-07d69ce07bfe5628f`，其他区按名字查最新） | |
| `DATA_VOLUME_GB` | 300 | |
| `GPU_HOURS_ALERT/SOFT/HARD` | 12 / 20 / 24（8 卡：9 / 11 / 12） | 用户在 UI 设预算 → 换算小时 |
| `MODEL_ID` | `Qwen/Qwen3.5-2B` | |

所有资源打 `Project=$EXP_TAG`，清理=一次 tag 查询。平台应额外打 `RunId`、`Owner` tag。

---

## 2. 端到端状态机

```
                                   ┌──────────── 一次性/项目级 ───────────┐
[DRAFT] → S1 PREFLIGHT → S2 AGENT_RESOURCES → S3 IMAGE_BUILD → S4 RUNTIME_DEPLOY → S5 RUNTIME_SMOKE
                                                                                         │
            ┌──────────────────────────── 每次训练 run ────────────────────────────────┘
            ▼
 S6 TRAINER_IAM_SG → S7 UPLOAD_CODE → S8 SPOT_LAUNCH ──(无容量)──► CAPACITY_WAIT / 换区/换型
                                         │
                                         ▼
                      S9 BOOTSTRAP(user-data) → S10 ENV_SETUP → S11 VLLM_SANITY → S12 SMOKE_TRAIN
                                                                                       │
                                                                                       ▼
                       ┌──────────────────── S13 TRAINING ◄───────── S15 RESUME ◄──┐
                       │  (watchdog / ckpt-sync / spot-watch 并行)                  │
                       │                                                           │
                       ├── spot 中断 ─► S14 INTERRUPTED(ckpt 已同步) ───────────────┘
                       ├── 20h 软限 ─► STOPPING(SIGTERM) ─┐
                       ├── 24h 硬限 ─► SELF_TERMINATED ───┤
                       └── 完成/用户停止 ─► S16 STOP_TRAIN ┴─► S17 TERMINATE → S18 EVAL/EXPORT → S19 CLEANUP → [DONE]
```

每个状态失败 → `FAILED(<state>)`，保留可重试；**任何会计费的状态（S8 起）失败时平台必须先走 S17**。

### S1 PREFLIGHT（只读，必须先过）
- 输入：region、trainer_region、instance_type、预算。
- 调用：
  - `aws sts get-caller-identity`
  - `aws service-quotas get-service-quota --service-code ec2 --quota-code L-7212CCBC`（**P 系列 spot vCPU**；REPORT 踩坑：`L-3819A6DF` 是 G/VT 系列，research 文档里写错了）
  - `aws ec2 describe-spot-price-history --instance-types p5.4xlarge --product-descriptions Linux/UNIX --start-time <now>`（各 AZ 最新价）
  - 可选 `aws ec2 get-spot-placement-scores`（PLAN 提到，脚本未实现）
  - `aws ec2 describe-instances --filters Name=tag:Project,Values=$TAG Name=instance-state-name,Values=pending,running,stopping,stopped` + `describe-spot-instance-requests --filters ... state=open,active` → 有则拒绝（**最多 1 台**护栏）
  - **止损通道自检**（REPORT 最贵的教训，第一台实例空烧 6.77h≈$137）：确认平台身份确实能执行 `ec2:TerminateInstances`、`ssm:SendCommand`、`ec2:AuthorizeSecurityGroupIngress`、`s3:PutObject`。可用 `aws ec2 terminate-instances --dry-run --instance-ids <fake>` / `iam simulate-principal-policy` 校验。
- 输出：选定 AZ、价格、配额余量。时长 < 30 s。
- 失败：配额不足（64 vCPU 挡住 48xlarge）→ 引导用户提额；止损权限缺失 → 硬阻断。

### S2 AGENT_RESOURCES（`agent/create_resources.sh`，幂等）
- 调用：
  - `s3api head-bucket` / `create-bucket --create-bucket-configuration LocationConstraint=$REGION`
  - `s3api put-public-access-block`（四项全 true）、`put-bucket-tagging`
  - `s3api put-bucket-lifecycle-configuration`：`$EXP_ID/` 14 天过期（rollout JSON），`smoke/` 1 天过期；`ckpt/`、`ledger/` 保留
  - `iam create-role`（trust `bedrock-agentcore.amazonaws.com`，条件 `aws:SourceAccount` + `aws:SourceArn` 限本账号本区）+ `iam put-role-policy --policy-name AgentCoreRuntimeAccess`
- 输出：bucket、`ACR_ROLE_ARN`。时长 < 1 min（IAM 传播另需 ~10 s）。
- 失败：本地安全策略拦截 `put-bucket-lifecycle-configuration`（REPORT），导致 smoke 前缀不过期。

### S3 IMAGE_BUILD（`agent/build_and_push.sh`）
- 步骤：`uv build --wheel -o agent/dist`（toolkit 本地 wheel）→ `ecr describe-repositories || ecr create-repository` → `ecr get-login-password | docker login` → `docker buildx build --platform linux/arm64 --build-context example=<repo>/examples/strands_math_agent -f agent/Dockerfile --push -t $ECR_IMAGE_URI` → `ecr describe-images`。
- Dockerfile 要点：`ghcr.io/astral-sh/uv:python3.13-bookworm-slim`，装 wheel + `strands-agents[openai]>=1.18.0 strands-agents-tools>=0.2.16`，COPY `rl_app.py reward.py models.py`，uid 1000，`EXPOSE 8080`，`CMD python -m rl_app`，**不加 otel wrapper**（冷启动更快）。
- 输出：镜像 URI（arm64，压缩 174 MB）。时长 3–10 min（取决于构建机是否原生 arm64）。
- 失败：基础镜像 uv 太旧解析不了仓库 `pyproject` 的 `override-dependencies` → 必须走 wheel；ACR 只接受 **linux/arm64**、镜像 ≤ 2 GB（不可调）；`strands_tools.calculator` 已 deprecated，必要时固定 `strands-agents-tools<0.9`。
- 平台化：用 CodeBuild（arm64 `ARM_CONTAINER` 环境）替代本地 docker；用户"上传 agent 代码"即触发。本地验证可选 `build_and_push.sh --local` + `agent/local_test.sh`（`/ping`、`/invocations` 立即返回 `{"status":"processing","result_key":...}`）。

### S4 RUNTIME_DEPLOY（`agent/create_runtime.sh`）
- 调用：
  - `bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?agentRuntimeName=='$NAME']|[0]"`
  - 不存在：`create-agent-runtime --agent-runtime-name ... --agent-runtime-artifact containerConfiguration={containerUri=$URI} --role-arn $ACR_ROLE_ARN --network-configuration networkMode=PUBLIC --protocol-configuration serverProtocol=HTTP --tags Project=$TAG`
  - 存在：`update-agent-runtime --agent-runtime-id ...`（同参数，换镜像）
  - 轮询 `get-agent-runtime --agent-runtime-id $ID --query status` 每 10 s，最多 60 次，直到 `READY` / `*FAILED*`
- 输出：`agent/runtime.env`（`AGENT_RUNTIME_ARN`、`AGENT_RUNTIME_ID`），日志组 `/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`。时长 1–3 min。
- 失败：`*FAILED`（镜像架构错、角色无 ECR 权限）；`list` 分页导致"查不到已存在 runtime"→ 平台应用 `--next-token` 全量翻页或直接存 ID。

### S5 RUNTIME_SMOKE（`agent/smoke_invoke.py`）
- 不依赖 GPU：`python smoke_invoke.py --bedrock --model-id openai.gpt-oss-20b-1:0 -n 3`（Bedrock OpenAI 兼容端点 `https://bedrock-runtime.<region>.amazonaws.com/openai/v1` + `aws_bedrock_token_generator.provide_token` 短期 token）。
- 链路：`RolloutClient.invoke(payload={"prompt","answer"}, input_id=..., api_key=...)` → `InvokeAgentRuntime` → agent 后台跑 → 结果写 `s3://$BUCKET/{exp_id}/{input_id}/{session_id}.json`（`app.py:223`）→ client `HeadObject` 轮询 → `GetObject`。
- 判据：`status_code==200` 且 `rewards` 存在。实测单条 3–7 s（含冷启动）；gpt-oss-20b 4/4 成功、reward 有 1.0。
- 失败/坑：
  - `RolloutClient.invoke()` 会覆盖 payload 中的 `_rollout`，`api_key` **必须**作为 `invoke(..., api_key=...)` 传。
  - toolkit 会把完整 payload（含 `_rollout.api_key`）写进 S3 → **不要用真实凭证冒烟**；Bedrock token 12h 有效，落在 `smoke/` 前缀，依赖 1 天过期规则。平台应把 smoke 写到独立前缀并强制生命周期。

### S6 TRAINER_IAM_SG（`infra/create_trainer_iam.sh`、`infra/create_sg.sh`）
- IAM：`iam create-role`(trust ec2) / `update-assume-role-policy`，`put-role-policy --policy-name trainer-inline`，`attach-role-policy AmazonSSMManagedInstanceCore`，`create-instance-profile` + `add-role-to-instance-profile`（同名）。
- SG：`ec2 describe-vpcs --filters isDefault=true` → `create-security-group --group-name agentcore-rl-trainer-sg` → `authorize-security-group-ingress IpProtocol=tcp,FromPort=18765,ToPort=18765,IpRanges=[{CidrIp=0.0.0.0/0}]`；**不开 22**。写 `infra/sg.<region>.env`（`TRAINER_SG_ID`、`TRAINER_SG_VPC_ID`）。
- 注意 SG 是**区域级**，TRAINER_REGION 换了要重建。时长 < 1 min。
- 失败：本地策略拦截 `authorize-security-group-ingress`（REPORT）。

### S7 UPLOAD_CODE（`infra/upload_code.sh`）
- `git ls-files -co --exclude-standard`（排除 `.venv`、`agent/dist`）→ `tar -czf` → `aws s3 cp ... s3://$BUCKET/code/repo.tar.gz`；`watchdog.sh`、`spot_interrupt_watch.sh` → `code/infra/`。
- 坑：`agent/runtime.env` 是 gitignored，**不会**被打包 → 实例上 `AGENT_RUNTIME_ARN` 缺失（REPORT）。平台应把 runtime ARN 直接写进 user-data 渲染的 `/etc/agentcore-rl.env`，不依赖文件。

### S8 SPOT_LAUNCH（`infra/launch_spot.sh`，`--dry-run` 只读预览）
1. 护栏：同 tag 实例 pending/running/stopping/stopped 或 open/active spot 请求 → `exit 2`。
2. 选 AZ：已有 `Project=$TAG,Role=data,status=available` 卷 → 用卷的 AZ；否则 `describe-spot-price-history` 最便宜；`--az` 可强制。
3. 数据卷：强制换 AZ 时 `create-snapshot` → `wait snapshot-completed` → 目标 AZ `create-volume --snapshot-id` → 旧卷改 tag `Role=data-old`。
4. 渲染 user-data：`envsubst '${ACR_S3_BUCKET} ${EXP_TAG} ${GATEWAY_PORT} ${GPU_HOURS_ALERT} ${GPU_HOURS_SOFT_LIMIT} ${GPU_HOURS_HARD_LIMIT} ${AWS_REGION}'`（**必须白名单**）。
5. `ec2 run-instances`：
   - `--instance-market-options '{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"one-time","InstanceInterruptionBehavior":"terminate"}}'`（one-time，避免 persistent 自动重拉绕过账本）
   - `--block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":100,"VolumeType":"gp3","DeleteOnTermination":true}}]'`
   - `--iam-instance-profile Name=$TRAINER_ROLE_NAME`
   - `--network-interfaces DeviceIndex=0,SubnetId=<default-for-az>,Groups=$SG,AssociatePublicIpAddress=true`
   - `--metadata-options HttpTokens=required,HttpEndpoint=enabled,InstanceMetadataTags=enabled`（IMDSv2 + 实例 tag 可从 IMDS 读）
   - tags：`Project`、`Name=...-trainer`、`Role=trainer`、`DataVolumeId`
   - `--user-data fileb://<rendered>`
   - `InsufficientInstanceCapacity|SpotMaxPriceTooLow` → 换下一个 AZ；全部失败 → `exit 6`（不自动升级 8 卡）
6. `ec2 wait instance-running` → 无卷则 `create-volume gp3 300GB 3000IOPS 250MBps` + `wait volume-available` + `create-tags DataVolumeId` → `attach-volume --device /dev/sdf` → `describe-instances` 取 `PublicIpAddress` → 写 `infra/instance.env`（`TRAINER_INSTANCE_ID/PUBLIC_IP/AZ`、`DATA_VOLUME_ID`）。
- 时长：1–3 min（有容量时）。
- 失败：
  - p5.4xlarge 在 us-west-2 **无 spot 容量**（REPORT 实际情况），最终用 us-east-2c 的 p5.48xlarge（≈$21/h，成本 ×8）。平台需要"容量等待 / 换区 / 换型（需用户确认成本）"分支。
  - 数据卷与 AZ 绑定：仅在同 AZ 重试；新建卷才跨 AZ。

### S9 BOOTSTRAP（`infra/user_data.sh`，cloud-init 首启 root）
- 装 awscli v2 / jq → 写 `/etc/agentcore-rl.env` → IMDSv2 读 `meta-data/tags/instance/DataVolumeId`，或 `describe-volumes --filters attachment.instance-id=<iid> tag:Role=data` → 最多等 10 min 出现 `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol<id 去横线>` → 无文件系统则 `mkfs.ext4 -L data` → fstab `LABEL=data /data ext4 defaults,nofail` → `mkdir /data/{hf,repo,gsm8k,ckpts,logs,ledger}` → 从 S3 拉 watchdog/spot 脚本到 `/opt/agentcore-rl/` → 装 systemd：`agentcore-rl-watchdog.timer`（`OnBootSec=1min`、`OnUnitActiveSec=5min`）、`spot_interrupt_watch.service`（`Restart=always`）。
- 日志：`/var/log/agentcore-rl-userdata.log`。时长 2–5 min。
- 平台判据：SSM `systemctl is-active agentcore-rl-watchdog.timer` + S3 `ledger/gpu_hours.json` 的 `updated_at` 在 6 min 内刷新 → 才允许进入下一步（**护栏生效确认**）。
- 失败（REPORT 首要坑）：`envsubst` 未白名单把脚本内 `${BUCKET}` 等替换为空 → `/data` 未挂载、watchdog 没装 → 成本护栏全部失效。

### S10 ENV_SETUP（`trainer/setup_trainer.sh`，经 SSM 以 ubuntu 运行，标记文件 `/data/.setup/<step>.done` 实现幂等）
- a. `nvidia-smi` 驱动 ≥ `580.65.06`（CUDA 13 verl 栈）
- b. 安装 uv；`UV_CACHE_DIR=/data/uv-cache`、`HF_HOME=/data/hf`
- c. `aws s3 cp s3://$BUCKET/code/repo.tar.gz` → 解压 `/data/repo`（`--refresh` 重拉）
- d. `uv sync --python 3.12 --extra verl`（CUDA13 wheel ~10 GB）
- e. `hf download Qwen/Qwen3.5-2B --local-dir /data/hf/Qwen3.5-2B`（公开无 gate，不嵌 HF token）
- f. `preprocess_gsm8k.py --output-dir /data/gsm8k` + pandas 切 `gsm8k_agent_test_200.parquet`（val）与 `gsm8k_agent_train_smoke.parquet`（64 行）
- 执行通道：`infra/ssm_run.sh <timeout> <cmd>` = `ssm send-command --document-name AWS-RunShellScript` + 轮询 `ssm get-command-invocation`。
- 时长：首次 20–40 min；复用数据卷时秒级跳过（venv/模型/数据都在 `/data`）。日志 `/data/logs/setup.log`。
- 失败：Ray uv-run hook 崩溃（`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` + 直接用 `.venv/bin/python`）；flashinfer JIT 需 ninja+nvcc（PATH 加 `/data/repo/.venv/bin:/usr/local/cuda-13.0/bin`）；SSM `send-command` 被本地策略拦截（REPORT）。

### S11 VLLM_SANITY（`trainer/vllm_sanity.sh`）
- `vllm serve /data/hf/Qwen3.5-2B --port 8001 --max-model-len 4096 --gpu-memory-utilization 0.4 [--language-model-only] --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3` → 等 `/health`（≤10 min）→ 一条带 calculator tool 的 chat completion → kill。
- 时长 3–8 min。日志 `/data/logs/vllm_sanity.log`。

### S12 SMOKE_TRAIN（`SMOKE=1 trainer/run_train.sh`）
- 覆盖：train=smoke parquet、`train_batch_size=8`、`ppo_mini_batch_size=8`、`n=4`、`total_training_steps=2`、`val_before_train=false`、`test_freq=-1`、`save_freq=1`、experiment 名加 `_smoke`。
- 门禁：`batching/total_real_rows>0`、`training/rollout_failure/total_missing_sessions≈0`、ckpt 落盘并同步 S3。实测 reward 0.25→0.55，missing 0。
- 时长 10–20 min（含 Ray/vLLM 启动 + flashinfer JIT）。

### S13 TRAINING（`trainer/run_train.sh` → `train_qwen35_2b.sh`）
- `setsid nohup train_qwen35_2b.sh > /data/logs/train_<ts>.log &`，PID → `/data/logs/train.pid`（watchdog 用于进程组 SIGTERM）；同时起 `sync_ckpt.sh` 循环（PID `/data/logs/sync_ckpt.pid`，每 300 s）。
- `/data/STOP_TRAINING` 存在则拒绝启动（`--force` 清除）。
- `GATEWAY_PUBLIC_HOST` 未设时由 IMDSv2 `meta-data/public-ipv4` 解析；必须导出 `AGENT_RUNTIME_ARN ACR_S3_BUCKET EXP_ID GATEWAY_PORT`。
- 环境变量：`VERL_USE_EXTERNAL_MODULES=agentcore_rl_toolkit.backends.verl.trainer`、`HYDRA_FULL_ERROR=1`、`TOKENIZERS_PARALLELISM=false`、`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0`、`CUDA_HOME=/usr/local/cuda-13.0`。
- 关键 Hydra 参数（平台"超参表单"的直接映射）：

| 组 | key | 值 |
|---|---|---|
| 模式 | `trainer.use_v1` / `trainer.v1.trainer_mode` | `true` / `agentcore_sync` |
| 算法 | `algorithm.adv_estimator` / `norm_adv_by_std_in_grpo` / `use_kl_in_reward` | `grpo` / true / False |
| IS 校正 | `algorithm.rollout_correction.rollout_is` / `rollout_is_threshold` | `token` / 2.0 |
| 数据 | `data.train_files` / `val_files` / `train_batch_size` / `max_prompt_length` / `max_response_length` | parquet / 32 / 2048 / 4096 |
| 数据类 | `data.custom_cls.path` / `name` | `pkg://agentcore_rl_toolkit.backends.verl.dataset` / `PayloadDataset` |
| 模型 | `actor_rollout_ref.model.path` / `use_remove_padding` / `enable_gradient_checkpointing` | `/data/hf/Qwen3.5-2B` / True / True |
| actor | `optim.lr` / `ppo_mini_batch_size` / `use_dynamic_bsz` / `ppo_max_token_len_per_gpu` | 5e-6 / 32 / True / 8192 |
| KL | `use_kl_loss` / `kl_loss_coef` / `kl_loss_type` / `entropy_coeff` | True / 0.001 / `low_var_kl` / 0 |
| loss | `loss_agg_mode` | `seq-mean-token-sum`（agentcore_sync 强制） |
| OOM 回退 | `actor.fsdp_config.optimizer_offload` / `param_offload` | env `OPT_OFFLOAD` / `PARAM_OFFLOAD`（默认 False）；再不行改 LoRA `lora_rank=32 lr=2e-5` |
| rollout | `name` / `mode` / `calculate_log_probs` / `max_model_len` / `temperature` / `tensor_model_parallel_size` / `gpu_memory_utilization` / `n` | vllm / async / true / 4096 / 1.0 / `${TP_SIZE:-1}` / 0.40 / 8 |
| val | `rollout.val_kwargs.n` / `temperature` / `do_sample` | 1 / 0.6 / True |
| agent loop | `rollout.agent.num_workers` / `default_agent_loop` / `agent_loop_config_path` | 1 / `agentcore_agent` / `trainer/agentcore_agent.yaml` |
| trainer | `n_gpus_per_node` / `nnodes` / `save_freq` / `test_freq` / `total_epochs` / `total_training_steps` / `val_before_train` / `resume_mode` / `logger` / `default_local_dir` | `${N_GPUS:-1}` / 1 / 10 / 10 / 1 / `${TOTAL_STEPS:-60}` / true / `auto` / `'["console"]'` / `/data/ckpts/$PROJECT/$EXPERIMENT` |

- `agentcore_agent.yaml`（`AgentCoreAgentLoop.__init__` kwargs，`agent_loop.py:97`）：`agent_runtime_arn ${oc.env:AGENT_RUNTIME_ARN}`、`s3_bucket ${oc.env:ACR_S3_BUCKET}`、`exp_id ${oc.env:EXP_ID}`、`max_tokens_per_turn 1024`、`tps_limit 8`（账号新会话 25/s 的 1/3）、`max_rollout_time 180`、`gateway_port ${oc.decode:${oc.env:GATEWAY_PORT}}`（需 int）、`gateway_public_host ${oc.env:GATEWAY_PUBLIC_HOST}`、`require_registered_sessions: true`。其余可选：`gateway_bind_host`、`max_pool_connections`、`max_turns_per_sid`、`history_mode`、`reward_mode`（仅 `built_in`）、`reward_thresholds`。
- 实测（8×H100 TP=2）：60 step 用 1h41m，**≈101–107 s/step**（`timing_s/gen≈85s`、`update_actor≈6s`、`testing≈67s`/次）；每 step 256 rollout。单卡 p5.4xlarge 计划值 3–4 min/step。
- 失败：ACR 502 / `RuntimeClientError`（零星，按 0 分计）；val `acr_failed` 5.5–8%；退出时 DataLoader atexit 报错（无害）。

### S14 INTERRUPTED / S15 RESUME
- 中断信号：`spot_interrupt_watch.sh` 每 5 s 轮询 IMDSv2 `meta-data/spot/instance-action`，HTTP 200 → `sync_ckpt.sh --once` + 跑一次 watchdog → 退出。平台侧可再订阅 EventBridge `EC2 Spot Instance Interruption Warning`（脚本未实现，推荐）。
- 续训：`infra/resume.sh` = `launch_spot.sh --resume`（要求存在 `Role=data` 卷，否则 `exit 3`）→ 同 AZ 重新申请 → user-data 挂回卷 → `setup_trainer.sh` 秒级跳过 → `run_train.sh`（`resume_mode=auto` 读 `latest_checkpointed_iteration.txt`）。卷丢失则 `trainer/restore_ckpt.sh`（`aws s3 sync s3://$BUCKET/ckpt/ /data/ckpts`）。
- 风险：spot 中断只给 2 min，单个 ckpt ≈28.6 GB（8 卡分片）可能同步不完 → 依赖上一次 300 s 循环同步；最多损失 `save_freq` 个 step。

### S16 STOP_TRAIN（`trainer/stop_train.sh`）
- 对 `train.pid` 进程组 `kill -TERM -- -<pid>` → 等 120 s → `SIGKILL` → `sync_ckpt.sh --once`。

### S17 TERMINATE（`infra/terminate.sh [--yes] [--snapshot] [--delete-volume]`）
- `describe-instances (Project,Role=trainer)` → 可选 `create-snapshot`+`wait snapshot-completed` → `cancel-spot-instance-requests` → `terminate-instances` + `wait instance-terminated` → 可选 `delete-volume`。默认保留数据卷（$0.08/GB·月，300 GB≈$24/月）。

### S18 EVAL / EXPORT
- 实验里**没有**单独跑 `evaluate.py`（`eval/` 目录不存在）；结果来自 verl 每 `test_freq` 在 200 题 val 子集上的 `val-core/unknown/reward/mean@1`。
- 产出：base 0.545 → step10 **0.85** → step60 0.795。REPORT 建议下一轮用 `evaluate.py` 在完整 GSM8K test 上比较 base/step10/step60，并降 lr（1e-6～2e-6）或提高 `kl_loss_coef`。
- 平台化：提供"选 ckpt → 起 eval 实例 / 或复用训练实例 → 产出 eval JSON"；HF 格式导出见 §7。

### S19 CLEANUP
- REPORT 遗留：两个 available 数据卷（≈$24/月 each）、VPC 私网资源（3 interface endpoint ≈$44/月）、S3 ckpt ≈172 GB（≈$4/月）、`smoke/` 含 token 的对象。平台应提供"资源账单视图 + 一键清理"（按 `Project` tag 查询 `describe-volumes`、`describe-vpc-endpoints`、`describe-snapshots --owner-ids self`、S3 前缀大小）。

---

## 3. 各角色 IAM 权限

### 3.1 ACR 执行角色（`AgentCoreRL-MathAgent-RuntimeRole`，`agent/create_resources.sh`）
Trust：`bedrock-agentcore.amazonaws.com`，条件 `aws:SourceAccount=<acct>`、`aws:SourceArn ArnLike arn:aws:bedrock-agentcore:<region>:<acct>:*`。

| Sid | Action | Resource |
|---|---|---|
| ECRImageAccess | `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` | `arn:aws:ecr:<r>:<a>:repository/<repo>` |
| ECRToken | `ecr:GetAuthorizationToken` | `*` |
| Logs | `logs:CreateLogGroup/CreateLogStream/PutLogEvents/DescribeLogStreams/DescribeLogGroups` | `log-group:/aws/bedrock-agentcore/runtimes/*`（脚本还给了 `log-group:*`，**过宽，平台应去掉**） |
| Metrics | `cloudwatch:PutMetricData` | `*`，条件 `cloudwatch:namespace=bedrock-agentcore` |
| Xray | `xray:PutTraceSegments/PutTelemetryRecords/GetSamplingRules/GetSamplingTargets` | `*` |
| WorkloadIdentity | `bedrock-agentcore:GetWorkloadAccessToken[ForJWT/ForUserId]` | `workload-identity-directory/default[/workload-identity/*]` |
| RolloutResults | `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` | 结果桶及 `/*`（平台可收窄到 `<bucket>/<run-prefix>/*`） |

### 3.2 训练机实例角色（`AgentCoreRL-Trainer-InstanceRole`，`infra/create_trainer_iam.sh`）
Trust：`ec2.amazonaws.com`；同名 instance profile；附加托管 `AmazonSSMManagedInstanceCore`。

| Sid | Action | Resource / 条件 | 用途 |
|---|---|---|---|
| S3ReadWrite | `s3:ListBucket` | 桶 | sync |
| S3Objects | `s3:GetObject/PutObject/DeleteObject` | 桶/* | code 拉取、ckpt/logs/ledger 写、rollout 结果 HEAD/GET |
| AgentCore | `bedrock-agentcore:InvokeAgentRuntime`, `StopRuntimeSession`, `GetAgentRuntime` | `arn:aws:bedrock-agentcore:<r>:<a>:runtime/*`（来源：`client.py` 的 `invoke_agent_runtime`/`stop_runtime_session`） | rollout |
| Ec2Describe | `ec2:DescribeInstances/DescribeVolumes/DescribeTags/DescribeSpotInstanceRequests` | `*` | watchdog、user-data 找卷 |
| Ec2MutateTagged | `ec2:TerminateInstances/CreateTags/AttachVolume` | `*`，条件 `aws:ResourceTag/Project=<tag>` | watchdog 24h 自毁 |

注意：ACR 资源在 us-west-2，而训练机可在 us-east-2 → `RUNTIME_ARN` 资源用的是 `AWS_REGION`（us-west-2）是正确的；但 EC2 权限无区域限定。平台化建议：`runtime/*` 收窄到具体 runtime ARN（含 `runtime-endpoint/*` 子资源）、S3 收窄到 run 前缀、`TerminateInstances` 加 `ec2:InstanceID == ${ec2:SourceInstanceARN}` 风格自限。

### 3.3 平台控制面角色（实验里由人工 admin 执行；平台后端需要的最小集合，按脚本调用汇总）

| 服务 | Action |
|---|---|
| STS | `GetCallerIdentity` |
| S3 | `CreateBucket`, `PutBucketPublicAccessBlock`, `PutBucketTagging`, `PutLifecycleConfiguration`, `HeadBucket/ListBucket`, `GetObject`, `PutObject`, `DeleteObject`（code 上传、ledger/日志/指标读取、清理） |
| IAM | `CreateRole`, `GetRole`, `UpdateAssumeRolePolicy`, `PutRolePolicy`, `AttachRolePolicy`, `CreateInstanceProfile`, `GetInstanceProfile`, `AddRoleToInstanceProfile`, `TagRole`, **`PassRole`**（给 ACR 角色 → `bedrock-agentcore.amazonaws.com`；给 trainer 角色 → `ec2.amazonaws.com`，用 `iam:PassedToService` 条件） |
| ECR | `CreateRepository`, `DescribeRepositories`, `DescribeImages`, `GetAuthorizationToken`, `InitiateLayerUpload/UploadLayerPart/CompleteLayerUpload/PutImage/BatchCheckLayerAvailability`（或交给 CodeBuild 角色） |
| AgentCore 控制面 | `bedrock-agentcore:CreateAgentRuntime`, `UpdateAgentRuntime`, `GetAgentRuntime`, `ListAgentRuntimes`, `DeleteAgentRuntime`, `TagResource`（create 时会建默认 endpoint，可能还需 `CreateAgentRuntimeEndpoint` / `CreateWorkloadIdentity`，需对照官方 IAM 文档确认） |
| AgentCore 数据面 | `InvokeAgentRuntime`（S5 smoke） |
| Bedrock（smoke 用） | `bedrock:CallWithBearerToken` / `InvokeModel`（短期 token 生成） |
| EC2 | `DescribeImages/Instances/Volumes/Subnets/Vpcs/AvailabilityZones/SpotPriceHistory/SpotInstanceRequests/KeyPairs/SecurityGroups/PrefixLists/RouteTables/VpcEndpoints`, `RunInstances`, `CreateTags`, `CreateVolume`, `AttachVolume`, `CreateSnapshot`, `DeleteVolume`, `TerminateInstances`, `CancelSpotInstanceRequests`, `CreateSecurityGroup`, `AuthorizeSecurityGroupIngress/Egress`, `RevokeSecurityGroupIngress/Egress`；VPC 模式另需 `CreateSubnet`, `ModifySubnetAttribute`, `CreateRouteTable`, `AssociateRouteTable`, `CreateVpcEndpoint`, `ModifyVpcEndpoint`, `DescribeVpcAttribute`；`iam:CreateServiceLinkedRole`（spot / AgentCore network SLR 首次） |
| SSM | `SendCommand`（`AWS-RunShellScript`，条件限 `Project` tag 实例）, `GetCommandInvocation`, `StartSession`（可选） |
| Service Quotas | `GetServiceQuota`（`L-7212CCBC`） |
| CloudWatch Logs | `FilterLogEvents/GetLogEvents`（读 ACR runtime 日志） |
| Cost Explorer | `ce:GetCostAndUsage`（COST.md 的 AgentCore MTD 检查） |

所有 EC2 写操作建议以 `aws:RequestTag/Project` + `aws:ResourceTag/Project` 条件收窄。

---

## 4. 网络：PUBLIC vs VPC

### 4.1 主方案（实际使用）：ACR `networkMode=PUBLIC` + 训练机公网 IP:18765
- 数据流：ACR 容器（Strands `OpenAIModel`，`base_url=http://<EC2 公网 IP>:18765/v1`，`api_key=sid`）→ 训练机 `RolloutGateway`（`gateway_bind_host=0.0.0.0`）→ vLLM token-in/token-out。
- SG：入向仅 `tcp/18765 from 0.0.0.0/0`（ACR PUBLIC 出口 IP 不可枚举；AMAZON 前缀数百条超 SG 规则配额）；无 22，管理走 SSM。
- 鉴权：gateway 默认对任意 Bearer `store.setdefault(sid, Session())` 隐式建会话（**无鉴权**）→ 必须 `require_registered_sessions: true`，仅接受 trainer `create_session` 过的 uuid4 sid，否则 401。端口只在训练进程存活期间监听。
- 成本：EC2→ACR 响应出流量 $0.09/GB，17k 会话 < $0.1；公网 IPv4 $0.005/h。
- 平台注意：明文 HTTP（无 TLS），sid 在公网传输；暴露面=2B 模型采样接口。生产化应考虑 TLS（ALB/NLB+ACM 或 gateway 自签）或改 VPC 模式。

### 4.2 加固方案（只完成到 Stage 1，未用于训练）：ACR `networkMode=VPC`
- `infra/create_vpc_private.sh`（训练机所在区 VPC）：2 个私有子网（无 IGW/NAT，须在 ACR 支持的 AZ ID，如 `use2-az1/az2/az3`，一个与训练机同 AZ）、私有路由表（脚本校验不含 `igw-`/`nat-`）、SG `agentcore-rl-acr-sg`（挂 ACR ENI）与 `agentcore-rl-vpce-sg`、interface endpoint `ecr.api`/`ecr.dkr`/`logs`（private DNS，需 VPC `enableDnsHostnames`）、S3 gateway endpoint（策略仅放 `prod-<region>-starport-layer-bucket/*`（ECR 层）+ 本区结果桶 `ACR_S3_BUCKET_PRIVATE=agentcore-rl-<tag>-<acct>-use2`）。写 `infra/vpc.<region>.env`。
- `infra/create_vpc_private_rules.sh`：ACR SG egress 18765→trainer SG、443→vpce SG、443→S3 prefix list，并撤销默认全开 egress；vpce SG ingress 443 from ACR SG + trainer SG；trainer SG ingress 18765 from ACR SG；`--lock-trainer` 再撤销 `0.0.0.0/0`。
- runtime：`--network-configuration '{"networkMode":"VPC","networkModeConfig":{"subnets":[...],"securityGroups":["<acr-sg>"]}}'`，`gateway_public_host`=训练机**私网 IP**。
- 约束：ACR runtime、VPC、结果桶需**同区** → 训练机在 us-east-2 时需在 us-east-2 另建 runtime + 桶 + ECR（或跨区复制镜像）；无 NAT 时 ACR 容器不能访问 Bedrock/公网工具；需要 `bedrock-agentcore` 的 network SLR。
- 成本：3 个 interface endpoint × 2 子网 ≈ $0.06/h ≈ $44/月（REPORT 遗留未删）。
- 平台建议：默认 PUBLIC（零基础设施、创建快），"企业模式"开关选 VPC；VPC 资源做成项目级长期资源而非每 run 创建。

---

## 5. Spot 生命周期与成本护栏

| 机制 | 实现位置 | 平台映射 |
|---|---|---|
| 单实例护栏 | `launch_spot.sh` 启动前 describe-instances + spot 请求检查 | 平台 DB 加锁 + 同样的 AWS 侧检查（防止 DB 与实际漂移） |
| one-time spot | `SpotInstanceType=one-time, InstanceInterruptionBehavior=terminate` | 固定 |
| 持久数据卷 | gp3 300 GB `Role=data`，挂 `/data`，root 卷 DeleteOnTermination | 每 run 一卷，run 结束后按策略快照/删除 |
| ckpt 同步 | `sync_ckpt.sh` 每 300 s `aws s3 sync /data/ckpts s3://$B/ckpt/` + `/data/logs → logs/`（不带 `--delete`，S3 保留所有 step） | S3 前缀改为 `runs/<run-id>/ckpt/` |
| 中断处理 | `spot_interrupt_watch.service` 5 s 轮询 IMDS `spot/instance-action` | + EventBridge 规则 `aws.ec2 / EC2 Spot Instance Interruption Warning` 通知平台把 run 置为 INTERRUPTED，自动触发 S15 |
| GPU 小时账本 | `watchdog.sh`（systemd timer 5 min）：下载 `ledger/gpu_hours.json` → upsert 本实例 `{instance_id, az, start=LaunchTime, end=now, hours}` → `cumulative_hours=sum` → 上传 | UI 直接读该 JSON 显示"已用/剩余 GPU 小时 + 估算费用"；`gpu_hours.sh` 是本地只读查账参考（固定 $2.63/h，8 卡需换价） |
| 12h 告警 | 写 `ledger/ALERT_12H`（一次） | 平台轮询 marker → 通知 |
| 20h 软限 | 写 `ALERT_20H`，`touch /data/STOP_TRAINING`，SIGTERM `train.pid` 进程组 | 自动进入 STOPPING |
| 24h 硬限 | `sync_ckpt.sh --once` → 上传账本 → `ec2 terminate-instances` 自身 | 实例自毁 = 最后防线，不依赖平台存活 |
| 多实例告警 | 发现另一台 running 同 tag trainer → `ledger/ALERT_MULTI_INSTANCE` | |
| 费用检查 | COST.md：手工 `ce get-cost-and-usage`；AgentCore MTD > $30 降 `rollout.n` | 平台可定时拉 CE（有 24h 延迟），实时值仍以账本为准 |

已知缺陷（平台实现时修正）：
- 账本是 S3 读-改-写，无并发控制；多 run 共用一个 key 会互相覆盖 → 每 run 一个 `ledger/<run-id>.json`，或改 DynamoDB 条件写。
- 账本以 `LaunchTime` 起算，按"实例小时"计（非 GPU 卡小时）；8 卡机价格 ≈$21/h，`gpu_hours.sh` 的 $2.63 估价会严重低估 → UI 用实际 spot 价（`describe-spot-price-history`）× 小时。
- 最后一次 watchdog 到终止之间有 ≤5 min 未入账（REPORT：约 5 min）。
- watchdog 依赖 user-data 正确渲染；首台实例因此失守 → 平台必须在 S9 校验账本心跳，否则立即 terminate。
- 平台侧应再加 AWS Budgets（账号原本无 Budget）+ CloudWatch 告警作为独立于实例的第二道护栏。

COST 参考：p5.4xlarge spot ≈ $2.52–2.63/h（on-demand $6.88）；p5.48xlarge spot ≈ $20.3–21/h；实际本实验 GPU 9.4h ≈ **$190**（首台 6.77h 空烧 $137 + 正式 2.6h $53）；AgentCore 约 1.6 万 rollout 预计 < $20；单卡计划总价 ≈ $42，上限 $106。

---

## 6. 训练进度 / 指标 / 日志如何给 UI

| 数据 | 位置 | 获取方式 | UI 用途 |
|---|---|---|---|
| verl 控制台日志（含全部指标） | 实例 `/data/logs/train_<ts>.log` → 每 300 s 同步到 `s3://$B/logs/`（实验里改名为 `train_full.log`、`train_smoke.log`） | S3 GetObject（近实时需 SSM `tail` 或改用 CloudWatch Agent 推 `/data/logs/*.log`） | 日志面板 |
| 每 step 指标 | 同上日志中的行：`(TaskRunnerV1 pid=…) step:N - key:value - key:value …`（带 ANSI 颜色码，值可能是 `np.float64(0.545)` 形式） | 正则 `step:(\d+) - (.*)`，按 ` - ` 切分，`(\S+):(?:np\.\w+\()?([-\d.e]+)` 解析 | 曲线 |
| tensorboard | 本实验 **未启用**（`trainer.logger='["console"]'`）。若设 `TRAINER_LOGGER='["console","tensorboard"]'`，verl 默认写 `$TENSORBOARD_DIR`，缺省为 `tensorboard_log/<project>/<experiment>`（相对 cwd=`/data/repo`） | 设 `TENSORBOARD_DIR=/data/logs/tb/<run>` 让 sync 一并上传；UI 用 tbparse 读取或直接托管 TensorBoard | 推荐方案：保留 console 解析为主，另开 tensorboard 或自定义 logger 写 JSONL 到 S3 |
| GPU 小时 / 费用 | `s3://$B/ledger/gpu_hours.json`（+ `ALERT_*` marker） | GetObject，5 min 刷新 | 预算条、告警 |
| setup / sanity 进度 | `/data/logs/setup.log`、`vllm_sanity.log`、`/data/.setup/*.done` | SSM / S3 | 环境准备步骤条 |
| ckpt 进度 | `s3://$B/ckpt/<project>/<experiment>/latest_checkpointed_iteration.txt` + `global_step_N/` | ListObjectsV2 | ckpt 列表、可续训点 |
| rollout 明细 | `s3://$B/<exp_id>/<input_id>/<session_id>.json`（含 `rewards`、`status_code`、完整 payload；14 天过期） | ListObjectsV2 + GetObject 抽样 | "轨迹浏览器"（注意含 `_rollout.api_key`，UI 需脱敏） |
| ACR 侧日志 | CloudWatch `/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT` | `logs filter-log-events` | agent 报错排查 |
| 实例状态 | `ec2 describe-instances`（state、PublicIp、LaunchTime）、spot 中断事件 | API / EventBridge | 运行状态徽标 |

推荐在 UI 显示的核心指标（键名为日志中的真实 key）：

| 类别 | key | 含义 / 实测 |
|---|---|---|
| 主曲线 | `val-core/unknown/reward/mean@1` | val 200 题准确率：0.545→0.85→0.795 |
| | `critic/score/mean` | 训练 batch 平均 reward（step10 0.836） |
| 健康 | `batching/total_real_rows`（应 = batch×n，如 256）、`batching/total_padding_rows` | 0 = 链路断 |
| | `training/rollout_failure/total_missing_sessions` | ≈0；持续 >0 = ACR 错误/网关不可达/超时 |
| | `val-aux/unknown/acr_failed/mean@1` | ACR 调用失败率（0.08 / 0.055） |
| 稳定性 | `actor/kl_loss`、`actor/pg_loss`、`actor/grad_norm`、`actor/entropy` | KL 从 65 涨到 ~210 → 提示 lr/KL 设置问题 |
| | `critic/advantages/zero_mean` | GRPO 组内全同分比例（0.54），高=学不到 |
| | `training/rollout_probs_diff_mean`、`rollout_corr/rollout_is_*`、`rollout_corr/kl` | vLLM↔FSDP 概率差 |
| 长度 | `response_length/mean`、`val-aux/num_turns/mean` | 219→515 |
| 性能 | `timing_s/step`、`timing_s/gen`、`timing_s/update_actor`、`timing_s/testing`、`perf/throughput`、`perf/mfu/actor`、`actor/perf/max_memory_allocated_gb` | ETA = (total_steps-step)×timing_s/step |
| 进度 | `training/global_step` | |

---

## 7. Checkpoint 与评估产出

- 本地：`trainer.default_local_dir=/data/ckpts/<PROJECT_NAME>/<EXPERIMENT_NAME>/`（默认 `qwen35_2b_gsm8k/qwen35_2b_grpo`，smoke 加 `_smoke`）；verl 本地只保留最近 2 个（REPORT），想保留中间好 step 需提前复制（实验手工放到 `/data/ckpts_keep/`）。
- S3：`s3://$B/ckpt/qwen35_2b_gsm8k/qwen35_2b_grpo/global_step_{10..60}/` + `latest_checkpointed_iteration.txt`；不 `--delete`，6 个 step 全保留，≈28.6 GB/step，共 ≈172 GB。
- 单个 step 目录结构（实测 global_step_60，8 卡 FSDP）：
  - `actor/model_world_size_8_rank_{0..7}.pt`（各 1.3 GiB，FSDP 分片权重）
  - `actor/optim_world_size_8_rank_{0..7}.pt`（各 2.1 GiB，优化器状态）
  - `actor/extra_state_world_size_8_rank_{0..7}.pt`（RNG/lr scheduler）
  - `actor/fsdp_config.json`
  - `actor/huggingface/{config.json, tokenizer.json, tokenizer_config.json, chat_template.jinja, processor_config.json}`（**只有配置，无 safetensors 权重**）
  - （通常还有 `data.pt` dataloader 状态）
- 导出可部署模型：需用 verl 的 model merger（`python -m verl.model_merger merge --backend fsdp --local_dir <step>/actor --target_dir <out>`）把 FSDP 分片合并为 HF safetensors（bf16 ≈4.6 GB）（⚠️ 该命令与 tensorboard 默认目录来自 verl 上游惯例，本机无 verl 安装、仓库内也无引用，**未核实**，实现前需对照 verl 版本确认）；平台应提供"导出 HF 模型 → S3 → （可选）Bedrock Custom Model Import / SageMaker 部署"。分片数与 world_size 绑定，续训必须同 GPU 数（单卡 ckpt 不能直接在 8 卡上 resume，反之亦然）——平台换实例类型时要提示。
- 长期保存建议（COST.md）：只留最终/最佳 step 的 merged bf16 权重，删 optim 分片与数据卷，长期 < $0.2/月。
- 评估：本实验唯一评估来源是 val 指标（日志 `val-core/...`）；`eval/` 和 `evaluate.py` 计划但未执行。平台评估任务应：选 ckpt → merge → vLLM serve → 通过同一 ACR runtime + `RolloutClient` 跑完整 GSM8K test（1319 题），输出 `eval/<run>/<step>.json`（accuracy、acr_failed、平均轮数）。

---

## 8. 对平台设计的要点结论

1. 资源分两层：**项目级**（S3 桶、ACR 角色、ECR 镜像、ACR runtime、trainer 角色、SG/VPC）与 **run 级**（spot 实例、数据卷、S3 前缀、账本）。前者一次性向导，后者每次训练的状态机。
2. 所有 shell 脚本都可以 1:1 翻成后端 Step Functions / 任务队列步骤；实例内步骤用 SSM `AWS-RunShellScript` 执行，**实例内护栏（watchdog、spot watch、24h 自毁）必须保留**，平台挂了也能止损。
3. 启动计费资源前强制 PREFLIGHT（配额 `L-7212CCBC`、spot 价格/容量、止损权限自检），启动后强制"护栏心跳校验"，失败即 terminate。
4. 配置注入不要依赖 gitignored 文件（`runtime.env`），统一通过 user-data → `/etc/agentcore-rl.env` / SSM Parameter Store。
5. 指标来源：短期解析 console 日志（已验证格式），中期在 verl 打开 tensorboard 或写自定义 JSONL logger 到 S3，UI 读 S3。
6. 安全：gateway `require_registered_sessions=true` 为必选项；rollout 结果 JSON 含 api_key → UI 展示脱敏 + S3 生命周期；ACR 角色 `log-group:*` 收窄；VPC 模式做成可选企业开关。
