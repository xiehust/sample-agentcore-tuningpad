# 云端冒烟测试记录（2026-10-12）

状态：**Spot 和获批的 p5.48xlarge 按需尝试都被 AWS 容量阻塞，均已收尾，完整训练链路仍未通过。** 获批范围与费用边界见 cloud-smoke-plan.md；本地详细资源清单位于 `.run/cloud-smoke-20261012/state.json`。下方保留各轮次证据；累计预算仍为 US$100，未增加机型/区域或创建新节点组。

## 范围与资源

- 账号 434444145045，区域 us-east-2；既有集群 cl-757fe13017 / rl-dev-2。
- 固定 toolkit 提交 7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6。
- 增量操作预算 US$100，估算 US$80 停止新增付费动作并收尾；不是 AWS 账单硬封顶。
- 本次新 Agent ag-63075f79ee；Runtime rt-3453ddb1d6；Trainer ti-cbebef12c5。
- 已请求 Run `run-c06127c799` / Job `job-cce5c0aedf`：1 节点 Spot、2 step、max_hours=1、Run budget_usd=60、max_retries=0、scale_down_after=true。AWS 未满足本次 Spot 请求，未观察到实例分配；已主动停止，Run=stopped、Job=cancelled、RayJob=null、台账 node_hours=0 / est_cost_usd=0。请求的 2 step 未执行；导出/推理/评估均未创建。

## 已完成

- 真实预检：两目标节点组 desired=0、ASG 实例列表为空；没有其他活动 Job/CodeBuild，目标池没有 Pending/Running Pod。guardian 未暂停且最近成功，原集群累计预算未耗尽；未调整其保护。
- Agent `job-651f0be0fe`：context/checks/build/probe/push 全部成功，证明应用默认冷源码缓存与真实 wheel/ARM64 镜像构建/推送链路可用。
- Runtime `job-30dac5951b`：VPC/V2 部署 READY，默认 smoke 返回 2/2 HTTP 200 和有效数字奖励；**两条奖励均为 0，仅协议验收通过，不声明答题质量通过**。AWS runtime ID 为 `tp_smoke_git_1012_gsm8k_rl_dev_2-vrssUvEn2m`。
- 模型检查：Qwen/Qwen3.5-2B compatible=true，schema=qwen3_5；Run preview 返回 fsdp_full/world_size=8，无 warnings。
- 费用负向测试：未确认费用的有效 Run 请求返回 HTTP 400/code=run.confirm_cost，之后列表确认未创建该名称的 Run。
- 复用数据集 train/val/test 三个 S3 Parquet 对象均 HeadObject 成功；FSx PVC Bound，RayJob CRD 可读。
- 真实浏览器以本会话独立 Chromium/CDP、全新 home 下 profile 启动成功，调试端口仅监听 loopback；未关闭 sandbox/修改权限，未连接他人会话。完成主要页面逐页导航、英文首页截图、中文切换和 Settings 区域切换；暂无捕获的 JavaScript 或 API 错误，桌面无整页水平溢出。Settings 实际重跑预检后显示 Git 缓存路径已验证（旧台账预检字符串与新结果有明确区别）。
- Trainer `job-7c89c04007` 与 CodeBuild `tuningpad-trainer-build:5411ec4b-3e11-487d-aedc-b2b3ab5b3256` 均成功；ECR 完整 SHA 标签存在，digest=`sha256:aeb6bf3f2dcc6e1968b4a46cdd5713cd7acd15652aa093a47e36bba69955acec`。Run image stage 已成功复用该 ready Trainer，无重复构建。
- `.run/cloud-smoke-20261012/watch_job.py` 的离线响应序列验证通过：GET 有界重试、遵守 Retry-After、写超时不重放；CodeBuild stop 的成功/已终态/超时后核对/身份不符场景均用 fake 验证，没有停止真实构建。独立监控以创建时间计 90 分钟，超时只停止已核实的本次构建并请求取消 Job。

## 发现的问题 / 限制

- 非阻塞 UI 问题：真实浏览器在训练向导“模型与参数”页的 390px 视口下，document.scrollWidth=439，步骤条及长预设按钮越界。截图 `.run/cloud-smoke-20261012/screenshot-1791767487211.png` 已目视确认；DOM 定位为 `.v2-steps` 和两个 `.v2-btn.sm` 长标签。本次先记录，不在付费冒烟过程中扩大为样式修改。
- 默认 AgentCore smoke 的两条奖励均为 0。当前契约只要求 HTTP 200 与数字奖励；strict GSM8K 提取还要求 `#### <数字>` 格式，因此不能仅凭 0 分断言数学错误，也不能宣称答案质量已验收。

## 容量阻塞与收尾证据

- AWS ASG `eks-ec2-p5-spot-cad083d6-d8c4-bdfe-e9f6-ba583fdf00a9` 在本次扩容后多次返回 `UnfulfillableCapacity`，不是只展示历史错误：01:13:47、01:13:58、01:14:09、01:14:20、01:15:25 UTC 的实际活动均 Failed，提示无法满足请求配置。无法仅凭该错误判定更细的供给/配置根因。
- 在原配置不变、相同拒绝反复出现且实例列表仍为空时主动停止，没有等待到 30 分钟上限、没有提高节点数或切按需。`POST /api/runs/run-c06127c799/stop` 被接受后，Job 实际达到 cancelled，Run 实际达到 stopped。
- EKS 两组均 ACTIVE/desired=0，ASG DesiredCapacity=0/Instances=[]，直接 EC2 按 ASG tag 查询 pending/running/stopping/stopped 均为空。g5 从未扩容。该证据不是仅依赖 UI 的“已停止”。
- 训练停在 capacity，submit/monitor/finalize 未执行；没有本次 RayJob、checkpoint、训练 rollout、模型导出、推理端点或评估。后续项目标为 BLOCKED，不能借旧产物替代。
- 本次 Runtime 的两个已知 smoke session 均被 data-plane StopRuntimeSession 回报已不存在；随后删除准确的云 Runtime，轮询确认 control-plane GetAgentRuntime 返回 ResourceNotFound。没有删除其他 Runtime。
- **静态发现的清理接口风险**：`routers/agents.py:246–263` 的 Runtime 删除先删云资源、再硬删数据库行，而 `models.py:122` 的 Run 外键没有级联删除；存在 Run 引用时会有 FK 冲突风险。本次未故意调用该端点制造 500。为保留 Run 历史，使用现有 `services.agents.delete_runtime` 删除云资源，确认消失后通过现有 `pipelines.agent.save_runtime` 将本次本地行标为 deleted、清空 runtime_id/runtime_arn，保留 FK 与历史。没有关闭外键检查、修改业务代码或删除 Run 数据。
- 最后确认无 queued/running Job，后端 health 正常。已保留新 Agent/Trainer 镜像、输入数据和测试记录；未删除既有资源。浏览器仅关闭本会话实例。

## 费用与限制

- Run 训练台账为 0，但**本次总费用不能记为 0**：实际发生了 CodeBuild、AgentCore smoke、S3/ECR 等用量。尚无完整 AWS 账单数据，不报告虚假的实际总价或余额。
- CodeBuild GetBuild：SUCCEEDED，2026-10-12T00:57:44.931Z 至 01:09:53.092Z，Python Decimal 计算墙钟耗时 728.161 秒（12.136016… 分钟），computeType=BUILD_GENERAL1_LARGE。墙钟时间不等于 AWS 最终计费时长；没有拿注释价格充当账单。
- Spot 轮次结束时查询到 us-east-2 p5.48xlarge Linux 按需公开报价 55.04 USD/节点小时（01:23 UTC）；当时尚未请求按需实例。后续用户已明确批准并完成了按需容量尝试，其结果与收尾见下节；累计 US$100 预算未增加。
- 默认 AgentCore smoke 只验证协议成功和数字奖励，不能证明答案质量或训练收益。单区域 FSDP 主链路尚被容量阻塞，未覆盖其他区域/策略组合。

## 按需追加轮次（2026-10-12）

用户在同区域单台 p5.48xlarge 按需、仍累计 US$100/1小时上限的明确摘要后回复“继续”。本轮复用上方两个 ECR 镜像，没有新 CodeBuild。预检确认 ec2-p5-od / ec2-g5-serve / ec2-p5-spot 均目标0、无实例与共用 Pod，guardian 正常；55.04 + 1.212 + 20（累计非GPU规划预留、并非实际账单）=76.252 USD 的规划包络低于 US$80 停新动作线。

- Runtime 逻辑行仍为 rt-3453ddb1d6；恢复 Job `job-0b63e5b94a` 成功，新的 AWS runtime ID 为 `tp_smoke_git_1012_gsm8k_rl_dev_2-f2BPFJ4l88`。本轮 smoke 协议 2/2 成功，奖励分别为 0、1.0；不能当作正式模型评估。
- Run `run-adcb84bf3a` / Job `job-bd6c375e0b` 在 02:42:05 UTC 请求创建，显式 confirm_cost=true；配置1台 p5.48xlarge、on_demand、2 step、max_hours=1、Run budget=60、max_retries=0、scale_down_after=true。
- AWS managed group 的 ASG 覆盖 us-east-2a/2b/2c，使用 balanced-best-effort；本次三可用区都有 `InsufficientInstanceCapacity` 拒绝，不是只配置了单一区域或引用历史消息。DescribeScalingActivities 本次时间窗统计 16 条，全部 Failed，无成功启动描述；EC2 按组标签查询所有状态，本次 LaunchTime 窗口内实例为空。
- 独立监控达到30分钟容量截止后请求取消；阶段时间 02:42:06.104633Z—03:12:23.058505Z，Python 计算 1816.953872 秒（30.282564…分钟），多出的时间为轮询/取消完成延迟，不能声称精确30分钟硬实时限制。Job=cancelled、Run=stopped，RayJob仍null，训练步骤未执行，台账node_hours/est_cost_usd均0。
- 收尾复核：三个相关组均 ACTIVE、EKS/ASG目标0、实例列表空；直接 EC2 查询 pending/running/stopping/stopped/shutting-down 也为空。没有申请g5推理实例、没有导出或评估资源。
- 按本轮Runtime创建时间过滤本Agent的smoke结果键，仅处理本轮两会话（均已不存在）。删除本轮云Runtime并等到 GetAgentRuntime 明确 ResourceNotFound；通过现有 helper 保留被两个 stopped Run 引用的本地行并标deleted/清空云ID，不关闭外键、不删除历史Run。最终无活动Job，后端health=200。
- GPU未获得容量，不应把计划的1节点小时写成实际用量；两轮仍有 AgentCore/S3/ECR 与前轮 CodeBuild 用量，未获得完整账单，不能称总费用为0。

### 后续候选，未授权实施

只读报价与 planner 核对：us-east-2 的 p5.4xlarge（单张80GiB H100）公开按需价为6.88 USD/节点小时，Qwen3.5-2B FSDP full 的规划 world_size=1、估算训练状态33.8GiB/GPU、warnings为空。EC2 offering 显示该区域支持该型号，但不证明即时容量。现有测试池是p5.48xlarge，改用小规格需要用户同意机型变化及新建/收尾临时节点组；本轮没有执行该候选，也没有增加累计US$100预算。

## 用户明确要求再次尝试 OD（2026-10-12 06:34 UTC）

用户回复“再试下od”，本轮仍使用原 p5.48xlarge / us-east-2 / ec2-p5-od，未改小规格、未新建节点组，也未增加累计预算。新鲜预检确认池子空闲、guardian 正常且历史预算未耗尽；ECR 两镜像 digest 与先前相同，未重新构建。

- 恢复 Runtime Job `job-25c3cc61cf` 成功，AWS ID `tp_smoke_git_1012_gsm8k_rl_dev_2-4Zp5ys6Pqs`；默认协议 smoke 2/2 成功，奖励为0、0，质量限制仍然适用。
- 请求 Run `run-f2885b158b` / Job `job-798628fa89`，名称 smoke-git-1012-od2，06:41:09 UTC 创建请求；配置不变：1台按需、2step、max_hours=1、Run预算60、max_retries=0、结束缩零。
- AWS 在三个 AZ 仍返回 `InsufficientInstanceCapacity`。此次窗口 ASG 统计16条失败活动，EC2 全状态查询在请求之后无新实例。容量阶段 06:41:10.322802Z—07:11:19.371786Z，计算得1809.048984秒（30.1508164分钟）；监控在30分钟截止后请求取消并等待实际终态，未无限重开。
- Run=stopped、Job=cancelled、RayJob=null、台账node_hours/est_cost_usd=0；submit/monitor/finalize未执行，仍没有训练/导出/推理/评估结果。
- 三个相关组均 ACTIVE、EKS/ASG目标0、ASG实例空；直接EC2查询无未终止实例。仅处理本轮创建时间之后的两条smoke会话，均已不存在；云Runtime删除后Get明确ResourceNotFound，本地引用行软标deleted，保留全部三个测试Run历史。
- 最后无活动Job、后端health=200；复用镜像与记录保留。没有新增CodeBuild或GPU实例用量，但本轮Runtime/model调用及存储等费用不应当作零；实际完整账单仍未知。
- 本次单次有界重试已结束，不会自动再创建Run。全链路测试继续标记容量BLOCKED，不声称通过。
