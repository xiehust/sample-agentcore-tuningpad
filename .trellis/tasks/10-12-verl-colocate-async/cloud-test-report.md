# 同池异步真实测试结果（2026-10-13）

## 最终结果：容量 BLOCKED，临时云资源已清理

- 已按批准的 US$20 / us-east-2 / 单台 p5.4xlarge On-Demand 配置执行唯一一次训练请求，未改机型、区域、节点数或追加预算。
- Run `run-dec23e4174` / Job `job-05313353ae` 于05:22:47Z被接受；参数为 Qwen3.5-2B/GSM8K/FSDP full/colocate_async，B=m=8、rollout_n=4、2step、max_hours=0.5、Run GPU估算预算10、max_retries=0、scale_down_after=true。Run内10美元不是本次总操作预算20美元。
- preflight/image通过，但 us-east-2a/2b/2c 的ASG活动均出现 InsufficientInstanceCapacity。整个本轮ASG记录12条Failed扩容活动；EC2按本轮独占ASG查询所有状态均为空。容量阶段计算时长903.609652秒（含监督轮询/取消延迟），监督仅请求取消一次。
- 最终 Run=stopped、Job=cancelled、rayjob_name=null、node_hours/训练估算费用=0。submit/monitor/finalize未执行，没有真实GPU训练步、optimizer更新或checkpoint证据；**AC5不能记通过**。
- Agent镜像导入成功，隔离 VPC/V2 Runtime部署及2条协议smoke成功，奖励为0、0；不作答案质量通过结论。
- 收尾Job `job-36d4c801ec` 成功。05:42:30.843335Z独立核验：临时nodegroup、LT `lt-07393b636a552b633`、ASG均不存在；EC2所有状态无本轮实例；AWS Runtime `tp_async_1013_gsm8k_rl_dev_2-ol6Em22bOa` 返回ResourceNotFound，两条smoke会话已不存在。本地Runtime行软标deleted并保留唯一Run外键历史。
- 本Run RayJob/RayCluster/Pod均为空；无活动Job。原 ec2-g5-serve/ec2-p5-efa/ec2-p5-od/ec2-p5-spot 目标均为0，guardian仍启用且最近成功05:40:09Z。共享IAM角色、原数据、镜像和历史记录未删除。
- 未重建镜像、未观察GPU实例用量，但Runtime/模型调用、API及保留存储费用未取得完整账单，不能称整个测试免费或US$20余额未变。没有发起第二次Run。

CPU修正后的最终父代理 `make verify` 于05:42:58Z exit0，所有后端/前端/语法/i18n门禁通过。CPU调度证据仍只证明资源可调度，不冒充GPU训练通过。操作监督和清理的所有后台任务均已终态。

只读检查曾因脚本使用错误封装名 `list_groups`/`core_api`、`labels` 参数失败；按源码改为 `list_tuningpad`/`k8s.core`/`label_selector` 后核验成功。这些失败未触发资源写入，且未被当作云资源故障或通过证据。

以下为按时间保留的历史过程，不代表当前活动资源。

## 最新进展（05:18 UTC）：CPU 修正已验证，隔离资源准备中

用户明确批准最小修正。`cpu-scheduling-report.md` 记录真实隔离 Ray 检验：原8存储单元无法调度；改为2单元后同步/同池异步均可调度，仍为原8CPU Pod且留2CPU余量。32组真实Hydra、514项后端测试及完整make verify通过；没有换依赖/机型/节点数。

05:17:25Z 已接受零容量临时组 ec2-async-h100-1013 的创建（Job job-82e55f3b12），以及隔离Agent ag-53ff11b529 的既有镜像导入（Job job-d213ba2bc6，成功）。05:17:57Z 已接受 VPC/V2 Runtime rt-0a8834feaf 部署（Job job-b2d563647e）。两个异步准备Job均已登记独占state并启动有界监督。此时尚未创建Run或请求GPU扩容。

## 历史预检：真实训练未启动，曾发现 CPU 调度冲突

用户已批准新增 US$20，且要求继续；未重置旧预算。已创建独占本地记录 `.run/async-smoke-20261013/state.json` 和 watch.py，但 agent/runtime/nodegroup/run 均为 null。没有新建 GPU、节点组或 Runtime，无本次云资源需要释放；未核对实际完整 AWS 账单，不能用此声称所有费用为零。

## 已核对

- 账号434444145045、us-east-2、cl-757fe13017/rl-dev-2。04:57 UTC 本地活动 Job 为空；guardian CronJob 未暂停，最近成功时间04:55:09Z。
- 固定 SHA FSDP镜像及 GSM8K Agent 镜像 digest 与批准计划一致；ti-cbebef12c5 为 ready，不需要也没有启动新构建。
- 数据集 ds-893b488556 为 ready。临时名 ec2-async-h100-1013 不存在，AWS Runtime名 tp_async_1013_gsm8k_rl_dev_2 无匹配；旧Agent/Runtime/Run未改动。
- watch.py 离线验证通过：有限GET重试、429 Retry-After、确定性错误暂停、写请求只发一次、歧义取消的独占意图防重放、容量/总时限/无训练进展停止判断。

## 阻塞证据与建议

`backend/app/render/train.py` 的 rayjob 以 `max(4, spec.vcpus - 8)` 设置 CPU requests/limits。p5.4xlarge 是16vCPU，生成的 head limit 是8CPU。没有显式 num-cpus 覆盖；KubeRay 文档说明默认从CPU limit得出Ray逻辑容量（https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/config.html#resources）。

精确 verl0.9.0 / TQ0.1.8 源码（私有 CPU 环境，仅只读）显示：
- verl/trainer/main_ppo.py:144 的V1入口强制启用TransferQueue；
- transfer_queue配置默认8个SimpleStorage单元，每个placement bundle占1CPU（transfer_queue/storage/bootstrap/simple_storage_bootstrap.py:35–44）；
- TransferQueueController明示num_cpus=1（transfer_queue/controller.py:935）；
- verl的训练资源池默认max_colocate_count=3，每GPU预留3CPU（verl/single_controller/ray/base.py:146–151,192–215）。

本地计算的最低需求为8+1+3=12CPU，已超过8CPU额度，还未算其他进程。不能把这种确定的逻辑资源缺口归因于AWS GPU容量，也不应直接申请收费GPU后等待已知卡点。此前Hydra/CPU注册测试未启动Ray资源池，不能覆盖这项可调度性检查。

建议先对单卡小机型收敛TransferQueue分片CPU预留并补完整Ray资源可调度性回归；不改变机型、GPU数、区域、模型、算法或US$20预算。不虚报Ray逻辑CPU绕过物理limit，不额外加GPU节点。该内部资源配置调整尚未实施，需确认后再修改、验证并执行原真实测试。
