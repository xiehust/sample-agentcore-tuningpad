# colocate_async 真实测试（US$20 已授权）

用户明确回复“批准新增 US$20 操作预算，按上述配置执行并收”，随后要求“continue”。本次获准按下述单 p5.4xlarge OD / us-east-2 / 2步同池异步配置执行并收尾，不沿用旧 US$100，也不授权增预算/换规格区域/重建 Trainer。授权后的实际资源与结果另记 `.run/async-smoke-20261013/state.json` 及 cloud-test-report.md；本地验证不冒充 GPU 通过。

## 拟议资源与用例

- us-east-2，优先复用已有 rl-dev-2 集群、GSM8K 数据及固定 SHA 的镜像；使用隔离的测试 Agent/Runtime，不覆盖原业务 Runtime。执行前重新核对账号、资源所有权及空闲状态。
- 单个临时 EC2/EKS managed node group，最多 1 台 p5.4xlarge 按需（1张80GiB H100）；此前 p5.48xlarge 没拿到容量，不能据此推定本次容量。本候选尚未分配实例，也不保证能分配。
- Qwen/Qwen3.5-2B、FSDP full、training_mode=colocate_async；候选 B=m=8、rollout_n=4、2 step、save_freq=1、关闭基线验证。保留 SDK 原有 async pause/update/resume 合同和 off-policy/drop 配置，不加 separate_async 或额外的独立采样资源池。
- 2026-10-12 15:51 UTC 只读 ECR 检查：fsdp-7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6 仍存在，digest=sha256:aeb6bf3f2dcc6e1968b4a46cdd5713cd7acd15652aa093a47e36bba69955acec。本计划不授权重建 Trainer；若执行时镜像不符或缺失，暂停确认。
- 只验证此最小异步训练与 checkpoint，不额外做同步 GPU 对照、故障注入、重启实验、模型导出或推理服务。

## 费用、时限与失败收尾

申请新的增量操作预算 **US$20**，估算达到 **US$16** 时停止新增付费动作并收尾；不是 AWS 账单硬封顶。包括此次 GPU、测试 Runtime/模型调用、临时节点相关用量；既有 EKS/FSx 基础费用不计入新增预算，保留镜像/产物可能继续产生存储费用。

同一时点 Pricing 只读报价为 p5.4xlarge **6.88 USD/节点小时**。Decimal 计算 1节点×0.5小时的 GPU 参考费用为 **3.440 USD**，不包括启动/释放延迟、AgentCore/Bedrock、存储/网络等，不能当总账单。

- compute.max_hours=0.5、max_retries=0、scale_down_after=true，保留成本确认、RayJob deadline、guardian、registered sessions、网络/IRSA 边界。
- 容量等待最多15分钟；独立监督计算窗口最多30分钟，到线即发出停止/收尾，释放可能有延迟。首次训练更新15分钟无进展或后续10分钟无更新则停止；不能因采样日志持续增长而无限补采。
- 不擅自切大规格、增加节点、换区域、重建镜像或追加收费重试；请求结果不确定先按唯一名称/ID核对，不能盲目重放写请求。
- 无论成功/失败，核实本次 Job/RayJob/Pod 结束、临时节点组归零且 EC2 实例终止、测试 Runtime/session 删除/停止；删除本次临时节点组，不影响既有组、共享角色或用户数据。保留测试日志/checkpoint及必要历史记录；Runtime硬删除与Run外键的已知风险需用保留历史的既有domain-helper方式处理，不关闭外键。
- 操作脚本若需后台监督，先离线演练确定性失败、429、超时后对象已存在等序列；只处理此任务独占资源，边界/所有权不明确即暂停并报告残留。

## 验收证据

记录 UI/API/spec、实际 RayJob/ConfigMap、镜像 digest、Toolkit/verl/TQ 版本；确认运行到 AgentCorePPOTrainerColocateAsync 及相应 async buffer/client 和同池 pause→actor update→权重同步/resume 路径。若现有日志不足，只采用事先审阅的最小临时诊断，不把配置字符串冒充运行证据。

必须有有效轨迹、非 padding 训练行、实际 optimizer 更新以及 checkpoint 内容证据；不要求随机产生非零 staleness，不承诺吞吐或质量提升。报告区分 PASS/FAIL/BLOCKED、请求量/实际量、估算/账单及未覆盖的 TQ 队列恢复限制。当前没有执行本申请中的任何 GPU/节点组/Runtime 操作。
