# TuningPad：无代码 Agentic RL 训练平台

## Goal

让用户在 Web UI 上，不写代码、不用手敲 AWS CLI 或 kubectl，就能完成 agent RL 的全流程：
- 管理 SageMaker HyperPod EKS 集群
- 选择或接入 agent 和数据集
- 配置模型、策略和超参
- 在集群上跑 verl GRPO 训练（支持单节点和多节点），实时查看曲线和日志
- 合并导出 checkpoint
- 在同一个集群上部署推理并做评估
- 释放资源

底层用 `../agentcore-rl-toolkit`（ART）。UI 和工程骨架沿用 `../sample-agentcore-launchpad` 的 V2 风格。

## Confirmed Facts（详见 research/）

- 训练链路：ACR 上的 agent 负责 rollout 和 reward，结果写入 S3；verl 训练器内置 rollout gateway（HTTP 18765）采集 token。两者只通过三样东西耦合：runtime ARN、S3 桶、gateway 回调地址。`experiments/qwen35_2b_gsm8k` 已经在 EC2 上跑通（val 从 0.545 提升到 0.795）。
- gateway 只能解析特定的 chat template，按哈希匹配，支持 qwen3、qwen3_5（含 Qwen3.6、Qwen3-Coder、Nemotron-3）、glm4moe、gptoss。
- HyperPod EKS（`hyperpod-eks-cluster.md`、`hyperpod-eks-workloads.md`）：
  - 前置条件：EKS 1.33/1.34，私有子网，HyperPodHelmChart。
  - 集群管理 API：`CreateCluster(Orchestrator.Eks)`、`UpdateCluster`（实例组扩缩）、`ListClusterNodes`、`DeleteCluster`。
  - 官方 CFN 可以一次性创建 VPC、EKS、FSx、IAM、HyperPod：`aws/sagemaker-hyperpod-cluster-setup/eks/cloudformation`。
  - verl 推荐用 KubeRay `RayJob` 运行。AWS 有官方样例 `awsome-distributed-training/examples/training/verl/hyperpod-eks/rlvr`，在 4×p5en 上验证过。
  - 网络：ACR 必须使用 **VPC 模式**，与集群在同一 VPC，直接访问 Pod IP:18765（VPC CNI）。ClusterIP 不可达。HyperPod 的 SG 在创建集群时就要包含 gateway 专用 SG，创建后不能再改。
  - 导出：`python -m verl.model_merger merge --backend fsdp|megatron` 可以作为 k8s Job 运行。
  - 推理：vLLM Deployment 加 internal NLB 提供 OpenAI 兼容 URL，ACR agent 在 VPC 模式下可以直接访问。HyperPod Inference Operator 是备选方案。
  - 节点自动恢复对 Ray 是透明的，但 Ray 作业的续训需要平台根据 checkpoint 重新提交。
- 账号现状（只读查询，2026-10-04）：
  - 三个区域都没有 EKS 或 HyperPod 集群。
  - "for cluster usage" on-demand 配额只有 **us-east-1** 有：p5×2、p5en×2、p5e×1、p4d×2，us-west-2 和 us-east-2 都是 0。
  - HyperPod Spot 账号总量为 0。
  - 单集群最多 20 个实例，整个账号最多 30 个。
  - Training plan 可以查询报价，例如 us-east-1 的 p5 1 台 24h 为 $1098。on-demand 的 p5 为 $66/h。
- 平台主机是 arm64，有 docker：agent 镜像可以在本机构建。trainer 镜像是 x86_64 的大型 CUDA 镜像，需要用 CodeBuild（x86）构建。

## Decisions

- D1 MVP 范围：预置模板加用户自带 agent。用户可以上传代码 zip（含 Dockerfile），也可以提供已有的 ECR 镜像。训练前必须通过契约校验和 ACR 冒烟测试。reward 在 agent 镜像内计算。
- D3 部署形态：单机单用户。使用本机 AWS 凭证，口令登录，SQLite 存储，保留 `workspace_id`。
- D4 后端：只支持 verl，模板和契约层做成后端无关的抽象。
- D6 模板：GSM8K Math 和 OfficeBench。需要通用化：多模型、多机型、分布式。训练机型**只考虑 P 系列**：p4d/p4de.24xlarge、p5/p5e/p5en.48xlarge、p6-b200/b300.48xlarge。（2026-10-05 修订）推理另外允许 G 系列（g5/g6/g6e），只能用于 EC2 节点组上的 vLLM，不进入训练、规划器和 HyperPod 实例组。
- D8（用户 2026-10-04）：**训练算力从自管 EC2 改为 SageMaker HyperPod（EKS 编排）**，新增 **EKS/HyperPod 集群管理**模块。**模型合并导出和推理部署也在这个集群上运行**。不再保留 EC2 训练路径。
- D9（技术决定，依据调研）：
  - 集群创建：支持两种方式。新建时调用官方 HyperPod EKS CloudFormation 栈（VPC、EKS、FSx、IAM、HyperPod，以及 system 实例组）；也可以导入已有的 EKS 加 HyperPod 集群。之后由平台通过 SageMaker API 管理实例组（增删、扩缩、training plan），通过 helm 或 k8s client 安装平台组件：KubeRay、AWS LBC、FSx PVC、命名空间、S3 访问身份。
  - 网络：ACR runtime 一律使用 VPC 模式，部署在集群所在区域、所在 VPC 的私有子网中。安全组规划为 `sg-acr → sg-hp-gw:18765`，EFA SG 单独设置。gateway 通过 `ray.util.get_node_ip_address()` 或 head Pod 的 IP 对外提供地址，并强制开启 `require_registered_sessions`。
  - 训练：每个 run 对应一个 RayJob（K8sJobMode）。训练镜像有两个 profile：fsdp 和 megatron（含补丁），由 CodeBuild 构建并推送到 ECR。checkpoint 和模型缓存放在 FSx，导出时同步到 S3。多节点使用 EFA，`trainer.nnodes` 只计 worker 节点。
  - 容量：支持 on-demand（按配额）、Flexible Training Plan（平台内查询报价；购买属于高风险操作，必须二次确认）、Spot（实例组 `CapacityRequirements.Spot`，需要先提额）。
  - 成本护栏：HyperPod 节点只要存在就计费。平台提供以下保护，其中集群内的 CronJob 在平台宕机时依然有效：
    - 闲置 N 分钟后自动把 GPU 实例组缩到 0
    - run 结束后可选自动缩容
    - 预算上限
    - 集群内的 guardian CronJob，运行在 system 节点上，通过 IRSA 或 Pod Identity 获得 `UpdateCluster` 权限
  - 导出：用 k8s Job 跑 `verl.model_merger`，产出 HF safetensors 写到 FSx，再上传到 S3。
  - 推理：使用 vLLM Deployment，配合 internal NLB 和 `--api-key`，部署在推理实例组或者训练实例组空闲时的节点上。评估使用 `RolloutClient.run_batch` 加 ACR，指向这个推理 endpoint。
  - 默认区域为 us-east-1（只有这里有配额）。ACR、桶、ECR 镜像都按集群所在区域部署。

- D10（2026-10-04）：ACR 部署使用 `platformVersion`，默认 `auto`：在支持的区域（us-east-1/2、us-west-2、eu-west-1、ap-northeast-1）用 Runtime V2（快照启动），其他区域回退 V1；也可以通过 `agent_platform_version` 或在部署时显式选择。V2 的 create/update 要几分钟才 READY，部署前需等待 runtime 离开进行中状态。

## Requirements

1. **项目 Setup**：
   - PREFLIGHT 检查：身份、SageMaker cluster 配额（"for cluster usage" 和实例总数）、所需 IAM 权限、本机工具（kubectl、helm）。
   - 创建 S3 桶（带生命周期规则）、ACR 执行角色、平台使用的 EKS access entry。
2. **集群管理（EKS/HyperPod）**：
   - 新建集群（CFN 向导：区域、AZ、EKS 版本、FSx 容量、system 实例组），显示栈进度。也可以导入已有集群。
   - 集群详情页：状态、EKS 和 add-on 版本、平台组件安装状态（KubeRay、LBC、FSx PVC、guardian），提供一键安装或修复。
   - 实例组管理：新增或删除；设置机型（P 系列）、数量；容量来源（on-demand、training plan、spot）；深度健康检查开关；扩容和缩容（包括缩到 0）。
   - 节点列表：健康状态、所属实例组，可以手动替换节点。
   - Training plan：查询报价，购买需二次确认并显示金额，查看已有的 plan。
   - 配额视图：显示当前配额，附提额指引链接。
   - 删除集群：需要二次确认，同时删除 CFN 栈。
   - 闲置自动缩容策略和预算设置。
3. **Agents**：
   - 来源有三种：模板（GSM8K Math、OfficeBench）、上传 zip、ECR 镜像。
   - 流水线：build → push → local probe → 部署 ACR runtime（VPC 模式，绑定目标集群）→ 契约冒烟（Bedrock 上 OpenAI 兼容的模型）。
   - 显示阶段和日志，失败时给出可读的诊断，并支持重试。
4. **数据集**：
   - 支持上传 JSONL、CSV、parquet，校验后转换为 `payload` 列格式的 parquet（可选显式的 `prompt` 列），存到 S3。训练时由平台同步到 FSx。
   - 内置生成：GSM8K（含 200 题的 val 子集）和 OfficeBench（clone 仓库，上传到平台桶，按类别分层切分）。
   - 支持预览。
5. **模型与资源规划**：
   - 模型目录：预设模型加任意 HF id，做兼容性检查，显示参数量、是否 MoE、schema。
   - 机型目录：P 系列，显示规格、EFA 数、价格和配额。
   - 资源规划器：推荐训练策略（FSDP 或 Megatron，全参或 LoRA）、TP/CP/EP、节点数，估算显存并给出告警。
6. **训练向导**：依次选择 agent、数据集、模型、策略与超参（基础或进阶，模板提供预设）、算力（集群、实例组、节点数、是否 run 结束后自动缩容、小时和预算上限、自动续训），最后在确认页显示费用估算。
7. **训练执行**：
   - 状态机：PREFLIGHT（集群就绪、实例组容量、镜像存在）→ SCALE_UP（如需要）→ STAGE_DATA（S3 到 FSx，下载模型）→ SUBMIT_RAYJOB → TRAINING → 完成、停止或失败 → 可选 SCALE_DOWN。
   - 作业失败或节点被替换时，从 FSx 上最新的 checkpoint 自动重新提交（同拓扑，有次数上限）。
   - 用户可以 Stop、Resume。
8. **训练监控**：
   - 阶段时间线。
   - 指标曲线：val reward、critic/score、KL、response length、missing sessions、acr_failed。数据从 RayJob driver 日志解析。
   - 日志增量 tail。
   - Pod 和节点状态。
   - 已用节点小时和费用。
   - checkpoint 列表。
9. **导出**：选一个 checkpoint，用 k8s Job 合并为 HF 格式，上传到 S3，显示进度和产物位置。
10. **推理与评估**：
    - 把导出的模型（或基座模型）部署为 vLLM 服务（OpenAI 兼容，internal NLB，带 API key），可以扩缩和删除。
    - 评估作业：用 ACR agent 加 `RolloutClient.run_batch`，对已部署的 endpoint 跑 val 或 test 集，输出准确率和失败率，并支持在多个模型之间对比（例如 base 和训练后的模型）。
11. **资源与成本**：
    - 按 tag 或集群汇总：集群和实例组的节点小时、FSx、NLB、ACR runtime、ECR、S3 前缀，并估算月成本。
    - 支持确认后清理。
12. **概览仪表盘**，**登录**（口令；没有设置口令时只允许 loopback 访问），**中英文**界面。

## Acceptance Criteria

- [x] `make verify` 全部通过：backend ruff 加 pytest（hermetic）、frontend eslint、tsc、build、i18n key 一致性检查。
- [ ] `./start.py` 启动后，UI 为 launchpad V2 风格，可以切换中英文，未登录访问会被拦截。
- [ ] 单元测试覆盖以下内容：
  - 渲染器：RayJob、vLLM Deployment/Service、merge Job、ACR VPC 配置、agentcore_agent.yaml、CFN 参数，覆盖 FSDP/Megatron 和单/多节点组合，关键参数要与仓库中已验证的脚本一致。
  - verl 日志解析：使用实验中的真实日志样例。
  - run 状态机（打桩）：RayJob 失败后从 checkpoint 重新提交、节点替换、超出预算后停止并缩容。
  - 资源规划器：表驱动测试。
  - guardian 的缩容逻辑。
- [ ] 模型兼容性检查（拉取真实 HF 元数据）：Qwen3.5-2B、Qwen3.6-27B、gpt-oss-20b 判定为兼容；Llama 被阻止，并说明原因。
- [ ] 真实 AWS，不产生 GPU 费用：
  - Setup 幂等。
  - 机型和配额视图显示真实数据。
  - 能查询 training plan 报价。
  - 新建集群的 CFN 模板参数通过 validate-template。
- [ ] 上传不符合契约的数据集或镜像时，UI 显示具体错误，而不是 500。
- [ ] 需要用户批准费用，单独执行（(a)(b) 已完成；(c) 训练已跑通，EFA 传输还需日志确认）：
  - [x] (a) 创建 HyperPod EKS 集群，只含 system 实例组（CPU，低成本），安装平台组件；在集群 VPC 中部署 GSM8K 模板 agent（VPC 模式）并通过冒烟。
  - [x] (b) 1×p5（或 p4d）实例组扩容，跑 2 步的 GSM8K 冒烟训练：曲线有数据点，checkpoint 写入 FSx；导出 HF 模型到 S3；部署 vLLM 推理；对 base 和训练后的模型各跑一次小规模评估；最后缩容到 0。
  - [ ] (c) 可选，另行批准：2 节点 EFA 冒烟训练。
- [ ] 资源页能列出测试过程中产生的资源，并可以清理（包括删除集群）。

## 验证记录

只记录有真实运行证据的项，其余复选框尚未逐项核实。

- 2026-10-04 (a)：us-east-2 新建 rl-dev-2（CFN 栈 → 组件全部安装）。安装中途后端重启，helm release 卡在 pending-install，已修复为自动清理后重试。GSM8K agent 以 VPC 模式和 Runtime V2 部署（`get_agent_runtime` 返回 `platformVersion=V2`），冒烟 2/2。
- 2026-10-04 (b)：使用 1×p5.48xlarge Spot 的 EKS 托管节点组，而不是 HyperPod 实例组。Qwen3.5-2B fsdp_full 训练 2 步，reward 从 0.21 到 0.56，checkpoint 写入 FSx，跑完自动缩到 0（drain 用了约 10 分钟）。
- 2026-10-05 (b)：export 在 system 节点上合并出 4.4 GB HF 模型，同步到 S3（修复了 CPU 节点上的 NVML 报错）。vLLM 部署在 g5.2xlarge 上，走 internal NLB。50 条 val 评测（单轮 1024 token，temperature 0.6）结果如下：
  - base：mean reward 0.50，失败率 12%。
  - 训练第 2 步：mean reward 0.36，失败率 52%，失败原因都是单轮超过 max_tokens 被截断。
  - 训练后的模型反而变差，原因待查。
  - 评测结束后端点已删除，节点组已缩到 0。
- 2026-10-05 (c)：2×p5.48xlarge Spot 训练，使用 EFA 节点组 `ec2-p5-efa`（use2-az3，cluster placement group，每台 33 个 ENI）。
  - 2 步训练成功：world 16，reward 从 0.25 到 0.72，checkpoint 写入 FSx，GPU 费用约 $14。
  - 两个 Pod 都看到了 32 个 EFA 设备，`fi_info -p efa` 正常，aws-ofi-nccl 插件存在，`FI_PROVIDER=efa`。
  - **还没有日志证据证明 NCCL 实际走的是 EFA**：当时 `NCCL_DEBUG=WARN`，容器内也读不到 EFA 硬件计数器。现已在多节点下改为 `NCCL_DEBUG=INFO`，`NCCL_DEBUG_SUBSYS=INIT,NET`，下次多节点训练时确认。
  - 训练结束后 RayCluster 仍按 TTL 保留 600 秒，导致节点 drain 时 GPU 多计费约 10 分钟。已改为训练结束时立即删除 RayJob。
- 2026-10-05 清理：rl-dev（us-east-1）第一次删除时 VPC 被平台自建的 sg-acr/sg-nlb 卡住。修复后先删 SG（等待 AgentCore 释放隐藏 ENI），再删栈，最终状态为 `deleted`，VPC 已不存在。

## Out of Scope

- 自管 EC2 训练路径
- Tinker 和 slime 后端；SWE/OpenHands 这类 A2A 场景
- 多用户和多账号
- gateway TLS
- HyperPod Inference Operator / SageMaker endpoint 托管推理，作为后续选项
- Managed Tiered Checkpointing
- GB200 UltraServer（p6e）
- 非 P 系列 GPU 机型用于训练（G 系列仅用于推理，见 D6）
- 推理服务对公网开放
