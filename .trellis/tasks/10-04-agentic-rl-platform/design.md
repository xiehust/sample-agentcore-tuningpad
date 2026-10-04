# Design: TuningPad（HyperPod EKS 版）

调研依据：
- `research/launchpad-architecture.md`：UI 与工程骨架
- `research/toolkit-core.md`：ART 契约和参数
- `research/aws-experiment-workflow.md`：已验证链路、踩坑、指标
- `research/generalization-distributed.md`：模型、机型、规划器
- `research/hyperpod-eks-cluster.md`：集群、容量、存储、韧性
- `research/hyperpod-eks-workloads.md`：RayJob、网络、导出、推理
- `research/officebench-template.md`

## 1. 总体架构

```
浏览器 (React V2) ─/api─► FastAPI 控制面（本机 arm64, :8000, SQLite）
                            │ boto3: sagemaker / eks / cloudformation / bedrock-agentcore(-control) / s3 / ecr / codebuild / service-quotas / pricing
                            │ kubernetes python client（EKS token 由 STS 预签名生成）+ helm（subprocess，仅组件安装）
                            │ docker buildx（agent 镜像 arm64，本机）· CodeBuild（trainer 镜像 x86_64）
                            ▼
  ┌──────────────────────── 区域 R（默认 us-east-1）· 集群 VPC ────────────────────────────┐
  │ EKS 1.33/1.34 + HyperPod 集群                                                            │
  │  实例组: system(ml.m5.2xlarge×1) · gpu-<name>(P 系列, on-demand/plan/spot, 可缩到 0)      │
  │  命名空间 tuningpad: RayJob(训练) · Job(导出/数据准备) · Deployment+NLB(vLLM 推理)        │
  │  组件: KubeRay · AWS LBC · FSx CSI + PVC(/fsx) · S3 访问(Pod Identity) · guardian CronJob │
  │                                    ▲ Pod IP:18765（sg-hp-gw）    ▲ NLB:8000（推理）        │
  │ ACR runtime（VPC 模式，私有子网，sg-acr）── rollout 回调 / 评估调用 ──┘                    │
  │ S3 桶（结果/数据集/导出）· ECR（agent 与 trainer 镜像）· FSx Lustre（ckpt/模型/数据）       │
  └──────────────────────────────────────────────────────────────────────────────────────────┘
```

- AWS 和 k8s 是事实来源，SQLite 只存标识符、阶段进度、解析出的指标和日志游标。
- toolkit 源码路径通过 `config.toolkit_path` 配置，默认 `../agentcore-rl-toolkit`。它有两个用途：
  - 构建 agent 镜像时打成 wheel 放进去
  - 作为 trainer 镜像的构建上下文，经 S3 上传后由 CodeBuild 使用

## 2. 目录结构

```
backend/app/
  core/            config, db, errors{code,message,detail}, auth(口令 cookie)
  services/aws.py  唯一 boto3 工厂；services/k8s.py 唯一 k8s client 工厂（按 cluster 缓存，token 刷新）
  jobs/engine.py   阶段流水线引擎（幂等阶段、resume、cancel、JSONL 日志）
  pipelines/       setup · cluster(create/import/components/instance-groups) · agent · dataset · run · export · inference · eval · cleanup
  catalog/         models(HF 元数据 + resolve_schema_name) · instances(P 系列规格/价格/配额)
  planner.py       资源规划器（纯函数）
  render/          rayjob · train_cmd(Hydra 覆盖) · agent_loop_yaml · merge_job · vllm · cfn_params · guardian
  metrics/         verl 日志解析
  routers/         setup clusters agents datasets models runs exports inference evals resources auth meta
  tests/
frontend/          launchpad V2 外壳 + ui.tsx 移植；pages: overview clusters agents datasets models runs exports inference evals resources settings
templates/{gsm8k_math,officebench}/   template.yaml + agent/ + dataset 生成器 + 训练预设
trainer_image/     Dockerfile.{fsdp,megatron} + buildspec.yml（CodeBuild）
cluster_assets/    cfn 参数模板、kuberay/lbc values、fsx pv/pvc、guardian CronJob、namespace/RBAC
```

## 3. 领域模型（SQLite，所有表带 `workspace_id`）

| 表 | 关键字段 |
|---|---|
| project | default_region, bucket, acr_role_arn |
| clusters | name, region, source(create/import), cfn_stack, eks_name, hyperpod_arn, vpc_id, private_subnets, sg_efa, sg_gw, sg_acr, fsx_id, components(JSON 状态), idle_policy(JSON), budget |
| instance_groups | cluster_id, name, instance_type, count, capacity(on_demand/plan/spot), training_plan_arn, deep_health_check |
| agents / agent_runtimes | agent: source/template/image；runtime: agent_id, cluster_id, runtime_arn, status, last_smoke |
| trainer_images | profile(fsdp/megatron), toolkit_sha, region, image_uri, status |
| datasets | split 的 S3 key, rows, prompt_field, schema 样例 |
| runs | agent_runtime_id, cluster_id, instance_group, nodes, model, strategy/params(JSON), status, stage, rayjob_name, retries, node_hours, est_cost |
| run_metrics | run_id, step, key, value |
| exports | run_id, step, status, s3_uri |
| inference_endpoints | cluster_id, model_source(base HF / export), instance_group, replicas, tp, url(NLB), api_key_ref(Secrets Manager), status |
| evals | endpoint_id, agent_runtime_id, dataset, status, accuracy, acr_failed, details_s3 |
| jobs | type, target_id, status, stages(JSON), log 路径, error |

## 4. 作业引擎

沿用 launchpad 的 deploy pipeline 和 Skill Lab 模式：
- 阶段函数幂等，执行逻辑一律是"先查询实际状态，再决定动作"。
- 作业状态：`queued|running|succeeded|failed|cancelled|interrupted`。后端重启后从第一个未完成的阶段续跑。
- 日志通过 `GET /api/jobs/{id}/log?offset=` 增量读取。
- 前端轮询间隔：详情页 2.5s，列表页 8s。
- 长时间运行的对象（RayJob、Deployment）由后端的 reconciler 线程每 15–30s 同步一次 k8s 状态。

## 5. 集群管理

### 5.1 新建（CFN）
- 模板使用官方 `aws/sagemaker-hyperpod-cluster-setup/eks/cloudformation` 的 main-stack，固定到某个 commit，URL 放进 config。
- 参数由平台渲染，包括：
  - 区域、AZ
  - EKS 1.33
  - VPC CIDR，需要足够大：每台 P5 约占用 81 个 IP
  - FSx 容量
  - system 实例组 ml.m5.2xlarge×1（必须保证至少一个 ≥4xlarge 规格的节点用于 observability；如果不需要 observability，可以用 2xlarge）
  - HyperPodHelmChart 依赖默认开启
- 执行 `create-stack` 并轮询 events，展示到阶段条上，耗时约 30–60 分钟。
- 如果官方栈不能附加我们自己的 SG，就在 CFN 之前由平台先创建 `sg-hp-gw`、`sg-acr`，以参数形式传入或通过 `UpdateCluster` 的 `OverrideVpcConfig` 附加到 GPU 实例组。这一点需要在实现时核实。

### 5.2 导入
用户选择区域和已有的 HyperPod 集群，平台通过 describe 获取 EKS、VPC、子网、SG 和 FSx 信息，校验通过后登记。校验项：
- EKS 认证模式
- VPC CNI 版本
- HyperPodHelmChart 是否已安装

### 5.3 平台组件（幂等安装/修复，每一项都有健康检查）
1. EKS access entry：给平台 IAM principal 绑定 `AmazonEKSClusterAdminPolicy`，作用域限定在命名空间 `tuningpad`，并授予集群级只读权限。
2. 创建 namespace `tuningpad`，配置 Pod Identity 关联，S3 权限限定为平台桶。
3. KubeRay operator 1.7.1（helm）。
4. AWS Load Balancer Controller（helm，需要 IRSA 或 Pod Identity）。
5. FSx PV/PVC `fsx-claim`。
6. 安全组：`sg-hp-gw` 只放行来自 `sg-acr` 的 TCP 18765；NLB SG 放行来自 `sg-acr` 的 8000 端口。
7. guardian CronJob，每 5 分钟运行一次，部署在 system 节点：
   - 读取 ConfigMap 中的闲置策略和预算
   - 统计各 GPU 实例组上是否有 tuningpad 的 RayJob、Job 或 Deployment 在运行
   - 闲置超过 N 分钟，或累计节点小时×单价超过预算时，调用 `sagemaker:UpdateCluster` 把该组缩容到 0，并把账本写入 S3 `clusters/<id>/ledger.json`
   - 所需权限通过 Pod Identity 授予，限定到该集群的 ARN

### 5.4 实例组与容量
- 实例组的增删、扩缩通过 `UpdateCluster` 完成，删除组用 `InstanceGroupsToDelete`。
- 每个实例组可配置：
  - 机型：限 P 系列白名单
  - 数量
  - `OnStartDeepHealthChecks`
  - `TrainingPlanArn` 或 `CapacityRequirements.Spot`（Spot 要求集群开启 `NodeProvisioningMode=Continuous`）
- Training plan 页面：通过 `SearchTrainingPlanOfferings` 展示报价。购买需要经过一个确认弹窗，显示总价和时间窗口，然后才调用 `CreateTrainingPlan`。
- 节点列表来自 `ListClusterNodes` 加 k8s node 标签 `sagemaker.amazonaws.com/node-health-status`。替换节点通过 `BatchReplaceClusterNodes` 完成，或者直接删除后由集群自动补足。
- 配额：通过 `service-quotas` 查询 "ml.X for cluster usage"、spot 配额和实例总数上限。

### 5.5 删除
依次进行：二次确认 → 删除 tuningpad 命名空间下的资源（会释放 NLB）→ 删除 CFN 栈（仅限通过平台新建的集群）。导入的集群只解除登记。

## 6. Agent 流水线（VPC 模式）

- 来源和本机构建流程与之前相同：模板、上传 zip、ECR 镜像；阶段为 BUILD/PUSH/LOCAL_PROBE。
- DEPLOY：`create-agent-runtime`，配置 `networkMode=VPC`，`networkModeConfig={subnets: 集群私有子网, securityGroups:[sg-acr]}`，区域与集群相同。一个 agent 可以在多个集群各部署一个 runtime。
- ACR 访问 ECR、Logs、Bedrock 需要出网：CFN 默认带 NAT。如果是导入的私网集群，平台检查 NAT 或 VPC endpoint 是否存在，缺失时给出提示。
- CONTRACT_SMOKE：通过 Bedrock 的 OpenAI 兼容端点（出网走 NAT）发起 n=2 次调用，结果写入 `smoke/` 前缀。

## 7. 训练

### 7.1 trainer 镜像
- 两个 Dockerfile：`fsdp` 用于 `uv sync --extra verl`；`megatron` 在此基础上加 `--group verl-megatron` 和 megatron-bridge 补丁。
- 基础镜像为 CUDA 13，安装 EFA/NCCL 用户态库：使用 DLC 或 `aws-ofi-nccl`，具体版本参考 awsome-distributed-training 的 verl Dockerfile。
- 构建方式：toolkit 源码打成 tarball 上传到 S3，由 CodeBuild（x86 LINUX_GPU 或 LARGE 环境）构建后推送到 ECR `tuningpad-trainer:<profile>-<toolkit_sha>`。
- 首次训练前必须有可用镜像，平台会提示一键构建。

### 7.2 Run 状态机
```
PREFLIGHT → ENSURE_IMAGE → SCALE_UP(实例组 count≥nodes，等待节点 Schedulable) → STAGE_DATA(Job: S3→/fsx/runs/<id>/data, HF 模型→/fsx/models 缓存)
 → SUBMIT(RayJob) → TRAINING ⇄ RETRYING(RayJob Failed/节点替换 → 从 /fsx ckpt 重新提交, ≤ max_retries) → SUCCEEDED|STOPPED|FAILED → [SCALE_DOWN 可选] → DONE
```
- PREFLIGHT 检查项：
  - 集群状态为 InService，组件健康
  - agent runtime 与集群在同一区域且为 READY
  - 实例组机型与规划结果一致，配额足够
  - 预算估算已经由用户确认
- RayJob 渲染规则：
  - head 只用 CPU，部署在 GPU 节点上（与 worker 共用），或部署在 system 节点。实现时先按"head 与 worker 共节点，head 不申请 GPU 和 EFA"处理。
  - worker 副本数等于 nodes，申请 `nvidia.com/gpu: N` 和 `vpc.amazonaws.com/efa: <机型EFA数>`，挂载 `/fsx` 和 `/dev/shm`，配置 EFA/NCCL 环境变量。
  - `trainer.nnodes` 等于 worker 数。
  - 环境变量注入方式：`runtimeEnvYAML.env_vars` 加 container env，包括 `VERL_USE_EXTERNAL_MODULES`、`AGENT_RUNTIME_ARN`、`ACR_S3_BUCKET`、`EXP_ID`、`GATEWAY_PORT`。
  - 不设置 `GATEWAY_PUBLIC_HOST`，gateway 默认通告 Ray node IP，也就是 Pod IP，正好满足 VPC 模式的要求。
- 训练命令由 `render/train_cmd.py` 合并"固定参数、模板预设、规划器输出、用户覆盖"四层得到：
  - FSDP 使用默认 config，Megatron 使用 `--config-name ppo_megatron_trainer`
  - `default_local_dir=/fsx/runs/<id>/ckpt`
  - `resume_mode=auto`
  - `agentcore_agent.yaml` 放在 ConfigMap 中，并强制 `require_registered_sessions: true`
- 监控（reconciler）：
  - 读取 RayJob 的 `.status`
  - 以增量方式读取 submitter Pod 日志：保存 offset，同时持久化到 `/fsx/runs/<id>/logs` 和 S3
  - 用正则 `step:(\d+) - ...` 解析日志，结果写入 `run_metrics`
  - 记录 Pod 和节点状态
  - 节点小时数按实例组节点数乘以 run 持续时间计算，再乘以单价得到费用
- Stop：删除 RayJob CR，checkpoint 保留在 FSx 上。Resume：用同一 run 目录和同样的拓扑重新提交，`resume_mode=auto`。

## 8. 导出

k8s Job 使用 trainer 镜像和 CPU 节点（system 或 GPU 节点均可，需要较大内存），执行：
```
python -m verl.model_merger merge --backend <fsdp|megatron> --local_dir /fsx/runs/<id>/ckpt/global_step_N/actor --target_dir /fsx/exports/<id>/stepN
aws s3 sync /fsx/exports/<id>/stepN s3://<bucket>/exports/<id>/stepN/
```
完成后在 UI 上列出产物：HF 目录、大小和 S3 URI。

## 9. 推理与评估

- 推理：每个 endpoint 由一个 vLLM Deployment 和一个 Service 组成。
  - Deployment 使用固定版本的 `vllm/vllm-openai` 镜像（需要支持 Qwen3.5/3.6）。启动参数：`--model /fsx/exports/...`（或 HF id 与缓存）、`--tensor-parallel-size`、`--api-key <Secrets Manager>`，以及 `--enable-auto-tool-choice --tool-call-parser <按 schema> --reasoning-parser <按 schema>`。
  - Service 类型为 LoadBalancer，使用 internal NLB（`target-type: ip`），安全组放行 `sg-acr`。
  - 状态由 Deployment 是否 Ready 加 `/health` 检查得出。支持扩缩副本和删除。
- 评估：
  - 后端运行 `RolloutClient.run_batch`，指定 `base_url=<NLB>/v1`、`model_id`、`api_key`，选用一个 agent runtime（与集群同 VPC）和一个数据集 split。
  - 结果汇总后写入 `evals`，同时保存到 S3 `evals/<id>/`。
  - 支持多个 endpoint 的对比视图。

## 10. 前端页面

| 分组 | 页面 |
|---|---|
| 概览 | 仪表盘：集群和实例组状态、运行中的 run、本月节点小时和费用、Setup 引导 |
| 基础设施 | **集群**（列表、新建或导入向导、详情页：组件、实例组、节点、training plan、配额、闲置策略和预算） |
| 构建 | **Agents**（模板库、新建向导、详情页：各集群 runtime 与冒烟结果）、**数据集** |
| 训练 | **模型目录**（兼容性检查）、**训练任务**（向导、详情页：时间线、曲线、日志、Pod、费用、ckpt、导出按钮） |
| 部署与评估 | **模型导出**、**推理服务**、**评估**（对比视图） |
| 管理 | **资源与成本**、**设置** |

组件直接移植 launchpad 的 `ui.tsx`、`TrainCurve`、`JobLog`、`LaunchSequence`、`StageCard`，界面支持中英文。

## 11. 安全

- 登录口令通过 `TUNINGPAD_PASSWORD` 设置。未设置时只允许 loopback 访问。
- 平台的 IAM principal 在 k8s 中的权限只限 tuningpad 命名空间，外加只读权限。
- gateway 只在 VPC 内可达，安全组最小化，并强制开启 `require_registered_sessions`。
- 推理只通过 internal NLB 暴露，需要 API key，key 存放在 Secrets Manager。
- rollout 结果中的 api_key 在 UI 中脱敏显示，并依赖 S3 生命周期规则过期删除。
- 高风险操作都需要二次确认：购买 training plan、扩容 GPU、删除集群或实例组。

## 12. 关键权衡与待验证

- 不用 Step Functions，用进程内引擎，加上集群内的 guardian 做独立止损。
- 待验证项（实现时核实，必要时调整）：
  1. 官方 CFN 能否接收我们自定义的 SG，或者是否需要在创建后用 `OverrideVpcConfig` 附加。
  2. verl 0.9 中 gateway 所在节点的 IP 能否被 ACR 访问：Pod IP 来自节点 ENI，SG 为集群 SG。
  3. HyperPod 节点上能否正常使用 Pod Identity。
  4. vLLM 对 Qwen3.5/3.6 的 tool call 支持情况（#39056）。
  5. CodeBuild 构建 trainer 镜像的耗时和镜像大小，需要选择合适的 compute type。

## 13. 实现中的偏差（已落地）

- boto3 和 k8s 的客户端工厂放在 `app/core/aws.py` 和 `app/core/k8s.py`，没有放在 `services/` 下。
- S3 权限改用 **IRSA**（为集群补建 OIDC provider），没有用 Pod Identity。原因是 HyperPod 节点对 Pod Identity 的支持没有核实，而 IRSA 已在 AWS 官方样例中跑通。
- 集群安全组：HyperPod 集群创建后安全组成员不能再改，因此不再单独建 `sg-hp-gw`，而是在 CFN 栈创建的集群安全组上追加入站规则：`sg-acr` → 18765/8000，`sg-nlb` → 8000。
- CFN 栈创建完整的 HyperPod 集群，其中 system 实例组为 m5.2xlarge，`NodeProvisioningMode=Continuous`。GPU 实例组之后由平台通过 `UpdateCluster` 管理（会与 CFN 状态产生漂移，这是有意为之）。
- 推理服务的 API key 存在 k8s Secret 中，没有放 Secrets Manager。
- RayJob 拓扑：head 节点也是 GPU 节点，所以 `trainer.nnodes = 节点数`。submitter 使用 `rayproject/ray:2.56.0-py312`，运行在 system 节点上。
- 训练机内由脚本每 30s 把 `train.log` 和 `ckpt_index.json` 同步到 S3，后端按字节 Range 增量读取日志并解析指标。
- 训练镜像在 CodeBuild 上构建，规格为 `BUILD_GENERAL1_LARGE`（约 $1.2/h）。
- 端口改为 8100（API）和 5180（UI），因为本机 8000 已被其他应用占用。
- 平台额外支持一个"独立 PUBLIC 冒烟 runtime"：在没有集群时也能低成本验证 Agent 契约。
- 灵活训练计划（FTP）是可选的容量来源（集群实例组表单和训练向导里都能用）。可以从本区域列表里选，也可以直接粘贴 ARN。`GET /api/training-plans/check` 会校验计划状态（Active/Scheduled）、TargetResources 是否包含 hyperpod-cluster、实例类型、实例数，以及计划所在 AZ 是否在集群子网范围内。用计划的组按预付处理：估价、run 成本、guardian 预算都记 0。已有实例组扩容时保留原来的容量来源，换容量来源会报 `cluster.group_capacity_mismatch`；向导会按容量来源区分组名（`-spot`/`-ftp`）。
- 作业引擎 `_finish` 先执行 on_finish hook，再写入终态，这样外部看到 job 结束时资源状态已经同步。
