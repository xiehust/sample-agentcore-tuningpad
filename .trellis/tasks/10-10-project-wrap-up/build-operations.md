# 镜像更新操作记录 — 2026-10-10

## 授权与边界

用户明确授权下一步并允许构建。执行 AWS 只读盘点、镜像构建和 ECR 发布；不部署 AgentCore 运行时，不启动训练、GPU 扩容或资源删除。生产代码与 IAM/网络边界不变。

## AWS 盘点

2026-10-10 通过项目 `app.core.aws.client` 执行只读盘点，并核对 STS account 与现有 ledger 一致：

- `rl-dev-ue-1`（us-east-1）和 `rl-dev-2`（us-east-2）的 EKS 均为 ACTIVE。
- 已核对的 8 个 EC2 GPU 节点组，desired 总数与 ASG 实际实例总数均为 0；两套 HyperPod 各保留一个 CPU system 节点，总数 2。未据此声称基础设施费用为零。
- 两区 ECR trainer 仍为 `0142ae2250` 的 FSDP/Megatron 镜像，没有当前 revision `99ab5d7dee` 的镜像。
- 已登记的 OfficeBench 运行时仍指向 10-08/10-09 或更早的镜像；构建和发布新镜像不会自动更新这些运行时。
- 旧 `rl-dev` 的一个 GSM8K runtime 返回 ResourceNotFound；记录为陈旧引用，没有删除 ledger 或尝试重建该 runtime。
- 现有 CodeBuild 项目标签与角色通过核对，没有修改项目/IAM 配置，也没有重复启动已有镜像的构建。

## 构建与验证

来源：toolkit `99ab5d7dee340f8c1087643d142e768e544d7b39`，工作区干净；其生产代码包含已合并的 rollout failure policy 和 OfficeBench 空回复修复。选择最小构建范围：一份 OfficeBench ARM 镜像供两区复用，以及主区域 us-east-1 的 FSDP trainer。不重复构建 Megatron 或 us-east-2 trainer。

### FSDP trainer（已构建并验证）

- CodeBuild：`tuningpad-trainer-build:348448ba-a480-4330-97b0-07d4a2e7ae35`。
- 目标 ECR tag：`tuningpad-trainer:fsdp-99ab5d7dee`，us-east-1。
- 上传的是正常 trainer 打包器生成的源码，SHA-256：`224769ef9dbc6fe16c0053fbcc0e695c85a25bf62915fe24e3cb4a5df775ebf9`；S3 对象带项目标签及 source/revision metadata。
- 使用已有 CodeBuild 项目/角色；单次构建超时 60 分钟、排队上限 10 分钟。启动请求带稳定 idempotency token，进程内禁用 SDK 自动重放；恢复时先查目标镜像与历史 build，已有失败不自动重试。
- 最终 CodeBuild 状态 SUCCEEDED；从其 startTime 到 endTime 的总历时为 703.409 秒（11.72 分钟），不是 AWS 账单中的计费量，实际费用未查询。
- ECR digest：`sha256:376020318fab65015380e6aa7ecca8489f7ef5856c4bb9d08724ab41cd61f948`；已核对构建日志中的 push digest 一致，并下载源码对象复核 SHA-256 与 metadata。产物架构为 `amd64`，ECR 报告压缩大小 9,175,727,222 字节（8.55 GiB）。
- ledger `ti-3db2db78c6` 已为 ready，关联上述 build ID。没有启动 RayJob，也没有执行新失败策略的真实训练验收。

### OfficeBench agent（两区已发布，尚未部署运行时）

- 本地 ARM build 使用现有模板和 toolkit wheel，新的独立 context 不覆盖旧构建目录。
- 目标 tags：us-east-1 `tuningpad-agents:ag-98f339686d-99ab5d7dee`；us-east-2 `tuningpad-agents:ag-31d8456654-99ab5d7dee`。
- 镜像内 `rl_app.py`、`agent_loop.py`、`rollout_failure.py` 的 SHA-256 均与来源一致；本机 `/ping` 返回 200/Healthy，没有调用 rollout 或模型，临时 probe container 已清理。
- 两区 ECR 均确认 `arm64`；每个 tag 的 manifest digest 均为 `sha256:27aa97c76108dddfd26de652238e5091be33eb7d3a15443eda99dc763d51c1e4`，与本地验证镜像一致。每区 ECR 报告压缩大小 504,133,338 字节（480.78 MiB），不将两个 tag 误计为两次构建。
- 两个 Agent 记录已更新为新 URI/ready，带 toolkit revision、源码哈希和 probe 结果；没有更新 AgentRuntime。逐区核对完成，不把第一地区成功当作两区全部成功。
- Docker 29 本机的 `.Id` 是 manifest digest，不能把它当 config digest；已用现有镜像核实，发布校验使用 `.Descriptor.digest`。

本地操作脚本位于 `.run/cloud-build-inventory.py`、`.run/build-fsdp-refresh.py`、`.run/build-officebench-refresh.py` 和只读最终核验脚本 `.run/verify-image-refresh.py`。启动前完成自检：已有/失败构建识别、模糊启动结果不重放、健康探针重复拒绝暂停及源码哈希不符拒绝发布。独立只读审阅确认没有部署/扩容/训练路径；其指出的 trainer 来源证明不足已通过本次构建的源码校验和日志/ECR digest 比对补充。

此次实际构建两份不同镜像、发布三个 ECR tag：OfficeBench 一份复用到两区，FSDP 一份仅在 us-east-1。镜像大小不是新增计费存储量；未估算或声明实际账单费用。现有 Dockerfile 的浮动 base/OfficeBench ref 与部分未锁定依赖保持原样，因此不声称 toolkit revision 能单独重现全部镜像输入。

仓库 `make verify` 在构建完成后再次通过。仅任务/会话记录发生 Git 变更，操作脚本、构建 context 与运行 ledger 都保留在 gitignored 目录；未修改生产代码、依赖或 IAM/网络配置。

## 未完成事项

最终只读复核：`tp_officebench_smoke`、`tp_officebench_rl_dev_ue_1`、`tp_officebench_rl_dev_ue_1_obs` 和 `tp_officebench_use2_rl_dev_2` 仍为 READY 且指向旧镜像。本轮只完成镜像构建/发布，线上运行时尚未采用修复。

运行时部署、新失败策略真实训练验收、其余历史验证和资源去留仍需后续确认。本记录区分镜像已发布与运行时已使用，不能把构建成功当作 AC5 通过；父任务 AC4 的运行时采用/验证部分也保持开放。
