# OfficeBench 模板调研

来源：`../agentcore-rl-toolkit/examples/strands_officebench_agent/`，以及 `src/agentcore_rl_toolkit/backends/verl/examples/office_bench_agent/`。

## 与 GSM8K 的差异（平台必须支持）

| 维度 | OfficeBench | 对平台的影响 |
|---|---|---|
| 任务数据 | 来自 OfficeBench 仓库，共 300 个任务。需要先执行 `git clone https://github.com/zlwang-cs/OfficeBench`，再用 `preprocess.py` 上传到 `s3://<bucket>/officebench/<task>/<sub>/config.json`，每个任务另有一个 `testbed.tar.gz` | 数据集生成器要能执行 clone、上传、切分。ACR 执行角色需要对该前缀有 `s3:GetObject` 权限 |
| payload | 内容是 `{task_uri, testbed_uri}`，即 S3 URI，不含 prompt 文本 | parquet 需要额外包含一列显式的 `prompt` 列，由 `preprocess_officebench.py` 生成。平台的数据集 schema 要支持"预置 prompt 列"这种模式 |
| 切分 | 按 task 目录分层切分：val 占 0.2，seed 为 0，按类别（1/2/3 app）分层 | 生成器内置这套逻辑 |
| 镜像 | 依赖 LibreOffice、tesseract、ImageMagick，构建时 git clone apps。官方 Dockerfile 从 PyPI 安装 toolkit，并使用 otel | 平台的模板 Dockerfile 改为注入本地 wheel、去掉 otel。需要注意 ACR 的 2GB 镜像上限，构建后要校验 |
| reward | `OfficeBenchReward`，基于任务 evaluation 配置做比对，返回 0 或 1 | 在镜像内计算，平台无需额外配置 |
| agent loop | `max_tokens_per_turn 8192`，`max_rollout_time 1800`，`history_mode: linear`，`linear_on_nonlinear: reset` | 渲染器需要支持 `history_mode` 等 kwargs |
| 已验证配置 | Megatron LoRA（rank 64），Qwen3.6-27B，8 卡，TP4/CP2，128k 上下文，n=16，batch 16，参数/优化器 offload，`enable_thinking=false` | 需要 `uv sync --extra verl --group verl-megatron`，并在 CP>1 时打 `patches/apply-megatron-bridge-cp-clamp.sh` 补丁。这是第二套训练机环境 |
| 冒烟 | 可以用 Bedrock 上的 OpenAI 兼容模型调用工具链完成 | 契约冒烟流程与 GSM8K 相同 |
| 安全 | task 指令驱动 `shell` 工具。只能指向平台自己桶内的数据 | 数据集生成器只允许使用平台桶 |
