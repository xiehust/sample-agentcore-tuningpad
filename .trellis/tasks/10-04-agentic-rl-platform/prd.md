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
- [x] `./start.py` 启动后，UI 为 launchpad V2 风格，可以切换中英文，未登录访问会被拦截。
- [x] 单元测试覆盖以下内容：
  - 渲染器：RayJob、vLLM Deployment/Service、merge Job、ACR VPC 配置、agentcore_agent.yaml、CFN 参数，覆盖 FSDP/Megatron 和单/多节点组合，关键参数要与仓库中已验证的脚本一致。
  - verl 日志解析：使用实验中的真实日志样例。
  - run 状态机（打桩）：RayJob 失败后从 checkpoint 重新提交、节点替换、超出预算后停止并缩容。
  - 资源规划器：表驱动测试。
  - guardian 的缩容逻辑。
- [x] 模型兼容性检查（拉取真实 HF 元数据）：Qwen3.5-2B、Qwen3.6-27B、gpt-oss-20b 判定为兼容；Llama 被阻止，并说明原因。
- [x] 真实 AWS，不产生 GPU 费用：
  - Setup 幂等。
  - 机型和配额视图显示真实数据。
  - 能查询 training plan 报价。
  - 新建集群的 CFN 模板参数通过 validate-template。
- [x] 上传不符合契约的数据集或镜像时，UI 显示具体错误，而不是 500。
- [x] 需要用户批准费用，单独执行（(a)(b)(c) 均已完成）：
  - [x] (a) 创建 HyperPod EKS 集群，只含 system 实例组（CPU，低成本），安装平台组件；在集群 VPC 中部署 GSM8K 模板 agent（VPC 模式）并通过冒烟。
  - [x] (b) 1×p5（或 p4d）实例组扩容，跑 2 步的 GSM8K 冒烟训练：曲线有数据点，checkpoint 写入 FSx；导出 HF 模型到 S3；部署 vLLM 推理；对 base 和训练后的模型各跑一次小规模评估；最后缩容到 0。
  - [x] (c) 可选，另行批准：2 节点 EFA 冒烟训练。
- [x] 资源页能列出测试过程中产生的资源，并可以清理（包括删除集群）。

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
- 2026-10-05 (c) 复验（run-48dd594834，提交 080877b 之后）：
  - 2 步训练成功，reward 从 0.28 到 0.67，GPU 费用约 $14。
  - 训练日志证明 NCCL 走的是 EFA：
    - 加载了 aws-ofi-nccl 1.17.2。
    - `Selected provider is efa, fabric is efa-direct (found 32 nics)`。
    - `Using transport protocol RDMA`。
    - 通信器 `nranks 16`，rank 0 在 head 节点、rank 15 在 worker 节点。
    - 跨节点通道是 `via NET/Libfabric/<n>/GDRDMA`，即 GPUDirect RDMA。
  - 缩容：13:21:01 释放 RayJob，13:21:02 节点组缩到 0，13:22:35 两台实例进入 shutting-down。从训练结束到关机约 1.5 分钟，上次是约 10 分钟。
- 2026-10-05 其余验收项：
  - 登录：用 `TUNINGPAD_PASSWORD` 在 0.0.0.0:8199 起临时实例，未登录返回 401 `auth.required`，错误口令返回 `auth.invalid_password`，登录后 200。默认实例只绑定 loopback。
  - 中英文：用 headless Chromium 分别截取 en 和 zh-CN 的集群页，均为 V2 风格并正确切换语言。
  - 测试覆盖：新增节点替换的测试，覆盖 `POST /clusters/{id}/nodes/{node}/replace`（BatchReplaceClusterNodes，需确认，禁止替换 system 节点），以及节点被替换导致 RayJob 失败后自动从 checkpoint 续训、最终成功。控制台加了替换按钮。
  - 模型兼容性（真实 HF 元数据）：
    - Qwen3.5-2B：兼容，qwen3_5，2.27B。
    - Qwen3.6-27B：兼容，qwen3_5，27.8B。
    - gpt-oss-20b：兼容，gptoss，20.9B，MoE。
    - Llama-3.1-8B：不兼容，原因是 chat template 不在 gateway 注册表中。
  - 不产生 GPU 费用的 AWS 检查：
    - 对 us-east-2 重跑 Setup，结果 succeeded。
    - 机型和配额视图显示真实价格与配额，errors 为空。
    - training plan 能查到 p5×1、24h 的报价：$1098.42。
    - validate-template 声明 153 个参数，我们传入的 17 个参数都在其中。
  - 契约错误：
    - ECR 以外的镜像返回 400 `agent.bad_image_uri`。
    - 缺字段的 JSONL 报 `dataset.invalid`，指出 `row 0: missing required field 'question'`。
    - 非 JSON 文件报 `dataset.bad_json`。
    - 缺 Dockerfile 的 zip 报 `agent.contract`，列出具体原因。
  - 资源页：列出了集群、runtime、trainer 镜像、各 S3 前缀和 ECR 的用量及月费用。purge `smoke/` 删除了 2 个对象；对不允许清理的前缀返回 400 `resources.not_purgeable`。
- 2026-10-07 base 和训练后评测差距的分析：
  - 逐张量比对了 step-2 导出和 HF base 的权重，两者实际相同：中位相对差为 0，`A_log` 完全一致，最大差异只来自 `linear_attn.norm` 从 fp32 转成 bf16（相对差 2.3e-3）。原因是 lr 5e-6 跑 2 步的更新量低于 bf16 的精度，被舍入掉了。
  - 所以 0.36 对 0.50 的差距不是训练造成的。但截断率 26/50 对 6/50 差距太大，不是采样噪声，更可能来自导出产物的差异：去掉了 MTP 层、多了 generation_config、重写了 tokenizer 文件、vision model_type 变了。
  - 这一点尚未定论，记录在 spec `aws-k8s-operations.md` 中。下一步做 A/B：用 base 的 config、tokenizer 和 generation 文件部署导出权重，看截断率是否恢复。
  - 已修复：GSM8K agent 现在捕获 MaxTokensReachedException，返回 `rewards: 0` 和 `stop_reason: max_tokens`，不再以 500 失败。评测汇总单独统计 `truncated`，Evals 页面加了"截断"列。
  - 更正：Qwen3.5 的 chat template 默认关闭思考模式，之前说"开着思考模式"是错的。
  - V2 兼容性：OfficeBench 模板在模块加载时只做静态初始化，没有问题。`static_checks` 新增检查：模块顶层调用 uuid、random、secrets、os.urandom、time、datetime.now、getpid 时给出 V2 警告。另外有测试保证内置模板没有这类调用。TuningPad 不给 runtime 设置环境变量，V2 的 2.5 KB 环境变量限制对它不适用。
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
