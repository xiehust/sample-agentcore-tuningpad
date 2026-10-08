# 通用化：模型、机型（P 系列 + Blackwell）、分布式训练

## 1. 模型兼容性（由 gateway 决定）

gateway 对 tokenizer 的 `chat_template` 计算 sha256，然后在 `rollout_gateway/response_schemas.py:_TEMPLATE_HASHES` 中查找。必须命中，tool call 才能被正确解析。目前支持：

| schema | 模型族 |
|---|---|
| qwen3 | Qwen2.5 / Qwen2-VL、Qwen3（thinking）、Qwen3-Instruct-2507、Qwen3-VL |
| qwen3_5 | Qwen3.5（think/nothink）、Qwen3.6、Qwen3-Coder、Nemotron-3 Nano/Super/Ultra |
| glm4moe | GLM4-MoE（GLM-4.5/4.6 系列） |
| gptoss | GPT-OSS |

模板匹配是逐字节比对。fine-tune 模型只要原样沿用基座的模板也能命中。

**平台做法**：用户可以填写任意 HF id。后端拉取 `tokenizer_config.json`/`chat_template.jinja` 和 `config.json`（不下载权重），调用 toolkit 的 `resolve_schema_name` 判断兼容性。同时读取参数量、是否 MoE、层数和 hidden 维度，供资源规划器使用。不兼容的模型直接阻止，并说明原因。

仓库内验证过的模型：

| 模型 | 配置 |
|---|---|
| Qwen3-4B-Instruct-2507 | FSDP 全参 / LoRA / Megatron LoRA，8 卡 |
| Qwen3.5-2B | FSDP，1 卡和 8 卡 |
| Qwen3.6-27B | Megatron LoRA，TP4 CP2，8 卡 |
| Qwen3-Coder-30B-A3B | Megatron LoRA，TP4，含 EP，单节点和 2 节点（swe cluster 配置） |

## 2. 机型目录（只考虑 P 系列，其中包含 Blackwell 的 p6-b200/b300）

| 实例 | GPU | 显存/卡 | EFA | 备注 |
|---|---|---|---|---|
| p4d.24xlarge | 8×A100 | 40GB | 4×100G | 价格最低，A100 支持 CUDA 13（sm80） |
| p4de.24xlarge | 8×A100 | 80GB | 4×100G | 供给少 |
| p5.4xlarge | 1×H100 | 80GB | 无（只能单节点） | 适合单卡小模型，spot 经常缺货 |
| p5.48xlarge | 8×H100 | 80GB | 32×100G | 主力机型 |
| p5e.48xlarge | 8×H200 | 141GB | 32×100G | |
| p5en.48xlarge | 8×H200 | 141GB | 16×200G（EFAv3） | |
| p6-b200.48xlarge | 8×B200 | 约 179GB | 8×400G | 实验配置中 vLLM MoE 需要 `moe_backend=triton`（swe qwen3_30b_p6.yaml） |
| p6-b300.48xlarge | 8×B300 | 约 268GB | | |

GB200 UltraServer（p6e）只能通过 Capacity Block 获取，不在本期范围内。

**账号配额（只读查询，2026-10-04）**：

| 区域 | P spot | P 按需 |
|---|---|---|
| us-west-2 | 768 vCPU | 768 vCPU |
| us-east-1 | 768 vCPU | 768 vCPU |
| us-east-2 | 768 vCPU | 384 vCPU |

一台 48xlarge 是 192 vCPU，所以 spot 配额最多够同时开 4 台。

**购买方式**：
- spot（one-time）
- 按需
- **EC2 Capacity Block for ML**：用户填写 reservation id，或在平台内查询并购买。P5/P6 按需和 spot 经常缺货，多节点训练基本要靠这种方式。需要在 `run-instances` 中传入 `InstanceMarketOptions.MarketType=capacity-block` 和 `CapacityReservationSpecification`。

价格和具体的 EFA 数量以 AWS 实时 API 为准（`describe-instance-types`、`describe-spot-price-history`）。平台展示的数值都来自实时查询，不写死在代码里。

## 3. 训练策略与资源规划器

规划器的输入：
- 模型元数据：参数量、是否 MoE、专家数
- 上下文长度
- 机型：GPU 数和显存
- 节点数

规划器给出推荐，用户可以手动覆盖：

| 策略 | 适用 | 关键参数 |
|---|---|---|
| FSDP 全参 | 显存足够时（≤ 约 8B @8×80G） | 是否 offload，`ppo_max_token_len_per_gpu` |
| FSDP LoRA | 中等模型或显存紧张时 | `lora_rank`/`alpha`，lr 约 2e-5 |
| Megatron LoRA | 大模型、MoE、长上下文 | TP/CP/EP/PP，recompute，optimizer/grad offload，mbridge |
| Megatron 全参 | 多节点、大模型 | 同上 |

显存估算公式：
- 全参（bf16 加 fp32 Adam）：权重和优化器状态约 16 字节/参数，再加上激活。
- LoRA：权重约 2 字节/参数，再加上少量可训练参数。
- 估算结果用于给出警告，不会阻止提交。

rollout TP 根据模型大小和单卡显存推导。

训练环境分两种 profile：
- `fsdp`：`uv sync --extra verl`
- `megatron`：`uv sync --extra verl --group verl-megatron`，CP>1 时还要应用 `patches/apply-megatron-bridge-cp-clamp.sh`

## 4. 分布式（多节点）

toolkit 的现状：
- swe_agent 中有 `trainer.nnodes: 2` 和 `rollout.nnodes` 的 cluster 配置，包括混合部署和训练/rollout 分离（`cluster_trainer2_rollout1`、`cluster_trainer_rollout_n4`），以及 `agentcore_separate_async` 异步模式。
- Ray 集群需要手动 `ray start` 启动，并且每个节点都必须导出 `VERL_USE_EXTERNAL_MODULES` 和所有 `${oc.env:...}` 变量（`backends/verl/README.md:43`）。
- gateway 位于 AgentLoopWorker 所在节点，对外通告的地址默认是 **Ray node IP（私网）**。`gateway_public_host` 只能设置一个全局值。
- toolkit 自带的实验没有在 AWS 上跑过多节点。

平台需要实现的部分：
1. **集群启动**：
   - 同一 AZ，cluster placement group。
   - 每个节点都配 EFA 网卡（`InterfaceType=efa`），安全组允许自身全流量互通。
   - DLAMI 自带 EFA 和 NCCL。环境变量设置 `FI_PROVIDER=efa` 等。
2. **Ray 集群**：
   - 用户数据脚本中的 rank 0 节点执行 `ray start --head`，其余节点执行 `ray start --address=<head私网IP>:6379`。
   - 所有节点的环境变量文件内容一致。
   - 平台等待 `ray status` 中节点数达标后才开始训练，训练任务只在 head 节点上提交。
3. **网关可达性**：
   - 多节点时 ACR runtime 使用 **VPC 模式**，与训练集群同区域、同 VPC。gateway 使用默认的私网 node IP，不需要公网端口。
   - 单节点保持 PUBLIC 默认，VPC 模式可选。
   - VPC 模式需要私有子网、ECR/logs/S3 的 endpoint，以及 ACR 的安全组。参照 `infra/create_vpc_private*.sh`，作为项目级资源按区域创建一次。
   - 因为 ACR、桶和 VPC 必须在同一区域，agent runtime 要按训练区域部署（每个区域一个 runtime）。
4. **Checkpoint**：
   - 各 rank 的分片写到各自节点本地的 `/data/ckpts`。
   - 每个节点各自 `aws s3 sync` 到同一个前缀（不同 rank 的文件名不冲突，且不使用 `--delete`）。
   - 续训时每个节点先从 S3 恢复全部分片。
   - 分片数和 world size 绑定，续训必须使用相同的节点数和 GPU 数，平台会做校验。
   - FSx for Lustre 作为后续选项。
5. **故障**：
   - 任何一个节点被回收或丢失，整个 run 都会进入 INTERRUPTED，平台终止全部节点后整体重新拉起。
   - 多节点建议使用按需实例或 Capacity Block，spot 会给出警告。
6. **护栏**：
   - 每个节点都运行 watchdog。
   - 账本按 run 汇总所有节点的实例小时，费用按实例价格乘以小时数计算。
   - 小时上限针对 run 整体，任一节点检测到超限都会终止整个 run 带 tag 的所有实例（权限按 RunId tag 限定）。
