# OfficeBench 运行时更新 — 2026-10-10

## 授权与边界

用户批准将关联 AgentCore 运行时更新到已验证的新镜像并检查 READY。不调用模型，不进行 rollout/训练，不扩容 GPU，不创建或删除运行时；不使用失败后删建的部署回退。

## 预检与执行方式

- 固定目标：us-east-1 的 OfficeBench standalone smoke、训练运行时及其 `_obs` 评测副本，以及 us-east-2 的 OfficeBench 训练运行时。无其他 agent 或 GPU 资源。
- 目标镜像沿用本轮构建的 `*-99ab5d7dee`，ECR digest 固定为 `sha256:27aa97c76108dddfd26de652238e5091be33eb7d3a15443eda99dc763d51c1e4`。
- AWS 身份与 ledger account 一致，平台无 queued/running job。四个目标在初次预检时均为版本 1 / READY，DEFAULT endpoint 的 liveVersion 均为 1。
- 逐运行时保留角色、网络、协议、生命周期、metadata、平台版本及环境配置；更新前后比较除镜像 URI 外的配置指纹和完整标签指纹。网络子网/安全组列表按集合顺序规范化比较；不记录环境变量明文。
- standalone 继续使用 PUBLIC / V1，其他目标保持 VPC / V2；只有 `_obs` 保留其原有 OTEL 环境变量。
- 先记录 deployment intent，再单次 `UpdateAgentRuntime`；稳定 clientToken 包含实际请求指纹。模糊响应不重放；恢复先读真实状态，已采用目标镜像的 runtime 不再次更新。
- 读请求仅对限流/暂时故障有界退避并遵守 Retry-After；更新请求不自动重试。每个 runtime 设置 30 分钟轮询期限（在途 API 调用仍受独立超时约束），必须 runtime READY 且 DEFAULT endpoint READY/liveVersion 指向新版本，才更新 ledger 的新镜像状态。
- 本地脚本 `.run/deploy-officebench-refresh.py` 和固定计划 `.run/officebench-deploy-plan.json`；离线自检覆盖已更新状态复用、失败/漂移暂停、模糊响应不重放、DEFAULT 旧版本不得通过，以及旧 smoke 结果保留为历史。

### 已处理的 API 差异与审阅问题

首次执行中，standalone smoke 已更新并验证成功；第一个 VPC 更新被 `ValidationException` 明确拒绝。只读核对确认它仍为原版本 1 / 原镜像 / READY，未产生该次更新。

原因是 `GetAgentRuntime.networkConfiguration.networkModeConfig.requireServiceS3Endpoint` 返回 `false`，但新建于服务 rollout 之后的 agent 不允许在更新中显式传入此字段。服务错误对本运行时给出 2026-06-11 截止时间，SDK 文档也说明该字段只允许用于 rollout 前的旧 agent。本次修正仅在这些已确认新运行时的请求里省略不可更新字段；对返回配置的指纹仍包含它，因此必须继续验证其值保持不变。没有为了规避错误改变 S3 网络边界或删建运行时。

独立审阅还发现模糊更新响应前应先把 ledger 标为 deploying；已补齐并自检。若更新结果未知，ledger 不再继续冒称 ready；确定性参数拒绝且只读核对旧 runtime/default 路由未变时，仅恢复旧状态，不把新镜像写入 ledger。

## 结果

更新完成后，以独立只读脚本 `.run/verify-officebench-deployment.py` 再次核对：

| 运行时 | 区域 | 平台 / 网络（未变） | 新版本 | Runtime / DEFAULT | DEFAULT liveVersion |
| --- | --- | --- | --- | --- | --- |
| `tp_officebench_smoke` | us-east-1 | V1 / PUBLIC | 2 | READY / READY | 2 |
| `tp_officebench_rl_dev_ue_1` | us-east-1 | V2 / VPC | 2 | READY / READY | 2 |
| `tp_officebench_rl_dev_ue_1_obs` | us-east-1 | V2 / VPC | 2 | READY / READY | 2 |
| `tp_officebench_use2_rl_dev_2` | us-east-2 | V2 / VPC | 2 | READY / READY | 2 |

- 四个目标均采用各自区域的 `*-99ab5d7dee` 镜像；最终 ECR digest 仍为本次批准的 `27aa97c7…51c1e4`。
- 配置与标签指纹全部保持一致，包括省略回写的 S3 endpoint 标志；只有 `_obs` 具有其原有 OTEL 环境变量。没有将 standalone 从 V1 升级为 V2。
- 三个 AgentRuntime 行和 `_obs` 状态已同步为 ready / 新镜像；旧 smoke 内容保留在 `last_smoke.previous`，本次标记 `ready_only`，不展示旧结果为新镜像验证。
- 没有创建或删除运行时、停止会话、修改 IAM/网络配置、调用模型或启动 GPU/训练；先前已更新的 standalone 被复用，没有额外产生版本 3。
- 操作自检、后续独立审阅、最终 AWS/ledger 只读核验通过；仓库 `make verify` 通过。实际部署费用未查询。

接口差异和验收契约已记录到 `.trellis/spec/backend/agentcore-image-updates.md`。本轮未修改生产源码或依赖，操作脚本与包含指纹的计划留在 gitignored `.run/`。

## 后续验收

本轮 READY 与路由验证不替代模型调用、OfficeBench 任务质量或新失败策略 AC5 的真实训练验收。
