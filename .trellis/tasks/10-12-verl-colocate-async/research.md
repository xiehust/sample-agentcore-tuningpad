# 固定版本合同与验收证据

状态：本文保留实施前的研究时点结论。用户此后已批准代码/本地验证，当前实施与验收结果见 verification.md；云测仍待 cloud-test-plan.md 的单独费用批准。以下串行汇总 `verl-contract`、`integration-map`、`verification-design` 三份只读报告，并抽查关键源码；当时仅规划，未运行产品测试、服务、构建或云操作。结论不依赖远端 main，也不授权升级依赖。

## 1. 来源与版本

- `backend/pyproject.toml:34` 固定仓库 `https://github.com/xiehust/agentcore-rl-toolkit.git`，SHA **7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6**。
- 下文 `T` = `data/cache/toolkit/7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6`；`K` = `T/src/agentcore_rl_toolkit/backends/verl`。本轮只读核对 HEAD 相同，`git status --short` 为空；未改源码缓存。
- `T/pyproject.toml:105–108,137–138` 精确依赖 **verl 0.9.0 / TransferQueue 0.1.8**；`T/uv.lock:8658–8659,9166–9169` 提供锁定来源。
- `V` = verl 0.9.0 包内 `verl`，`P` = `V/trainer/ppo/v1`。可重现来源为锁内 [wheel](https://files.pythonhosted.org/packages/b1/48/279b03f544f8c1fe1c86a230ecac8a448a4fa80697a76543917205a1d8f8/verl-0.9.0-py3-none-any.whl)，SHA256 `6e9374db1e2ab346a0aafe9a202bee1b239a042def2f2ccd6db2a4cd04c6308b`。本轮校验已有 `/tmp/verlsrc/verl-0.9.0-py3-none-any.whl`，并核对所抽查 trainer、replay buffer、Hydra YAML 与已有解包文件逐字节相同；未下载或安装。
- `verl-contract` 另报告校验锁定 sdist（1,281,588 字节），SHA256 `41a7ebff5482a94fe1b0311ec48825fcb10123f9420801ca1b8e7e1d122f3f12`；本轮未重下。临时路径不是版本身份，后续复查须按锁内 URL/hash，不能依赖临时目录存续。前次 main 查询出现其他 SHA，不属于本设计。

## 2. trainer 与调度：已确定

| 合同 | 证据与结论 |
| --- | --- |
| 不是 rollout 标签 | `backend/app/render/train.py:33–49` 已固定 `rollout.mode=async` 但 trainer 为 `agentcore_sync`。`V/trainer/main_ppo.py:142–155` 按 trainer registry 选择类。 |
| alias 与配置子树分离 | `K/trainer.py:35–81` 注册 `agentcore_sync / agentcore_colocate_async`，构造时归一化到 `sync / colocate_async`；配置用 native 子树而非 alias 子树。 |
| 同池分时 | `P/trainer_base.py:733–758` 使用 `global_pool`；`V/workers/rollout/llm_server.py:549–567` 绑定 actor worker group。`P/trainer_colocate_async.py:25–59` 采样结束 abort/sleep，训练后更新权重/resume，不是同卡训练和推理 GPU 内核同时执行。 |
| 部分轨迹与旧样本 | `V/workers/rollout/llm_server.py:404–460` 续接 token/logprobs 和版本范围；每步权重同步不保证所有消费样本来自最新版本。 |
| staleness | `V/trainer/config/ppo_trainer.yaml:224–260` 默认 warmup=1、threshold=8、strategy=drop。`P/replay_buffer.py:503–558` 按当前步与 prompt 派发步之差淘汰；不是只看轨迹内部跨度。wait 会等待达到阈值的在途组，不选作首版默认。 |
| 纠偏 | 保留 `backend/app/render/train.py:47,169–170` 的 rollout logprobs、token IS 与既有阈值默认 2；IS 与过期淘汰是不同机制，不新增调优旋钮。 |

## 3. 批次、失败、恢复边界：已确定

- `K/trainer_mixins/variable_row_batching.py:38–113`：V1、actor-only、非 distillation、`seq-mean-token-sum`。sync 与 colocate_async 都要求 `parameter_sync_step=1`；否则 rollout 已 sleep，后续采样可能永等。设 B=train_batch_size、m=有效 mini_batch_size，必须为正整数且 B%m=0；分区数 B/m 不随轨迹扩行改变，按 DP×分区数 padding，loss 归一化用 m×rollout.n。
- 正整数前置校验是控制面的防御补足，不声称 SDK 已完整验证类型/零值。现有 `render/train.py:92–125,156` 缺 mini bounds，且 `mini or B` 会吞掉 0；仅补这组实际合同，保留省略/null 继承与其他参数的合法零值。
- `K/trainer.py:55–79` 明确 **RolloutFailureGuardMixin 只在 sync**。`P/agent_loop_tq.py:131–148` 等组内 session 全部结束再标记 failure；`P/replay_buffer.py:514–558` async 淘汰整个失败 prompt 组（含成功兄弟）并补采。持续失败可能一直补采，无 SDK drop-fraction 熔断；`missing_sessions=0` 不代表没有失败。不得复用 sync guard 的承诺或假造等价保护。
- `P/trainer_base.py:105–115,831–850,935–946` 队列快照/恢复/重派要求 TransferQueue≥0.1.9 且有对应 API；锁定 **0.1.8 关闭该路径**。actor/data checkpoint 路径存在，但在途与待消费样本不保证完整恢复；不升级依赖来掩盖缺口。现有 `render/train.py:230` 的 Megatron model-only 保存也不保证优化器连续性。
- `V/workers/engine_workers.py:134–142,720–804`（报告证据）共享 EngineRegistry/naive 权重同步，包含 full/LoRA 分支；支持统一渲染设计，不等于四种策略均经 async 硬件验收。保留 FSDP LoRA safetensors 与 Megatron LoRA merge（`render/train.py:218–220`、`backend/tests/test_runs.py:85–109`）。

## 4. 接入与报告分歧

- `integration-map` 建议顶层 `training_mode`；`verification-design` 建议 `params.trainer_mode`、null 清空。采用前者作为**待审批设计**：mode 是固定枚举运行合同，不是开放训练旋钮；不新增第二条入口。顶层省略→sync，显式 null/非法值→422；旧 spec 仅缺键回落 sync，非法持久值不能静默降级。数值 params 的 null 继承不适用于 mode。
- `backend/app/routers/runs.py:49–61,151–196`：preview/create 共用 `_validate`，新 Run 写 JSON spec；`models.py:128` 已有 JSON，无需迁移或回填。`services/runs.py:43–61` 从 spec 渲染；`pipelines/run.py:291–297,443–448` 重试重读 Run；`routers/runs.py:293–301` resume 复用同 Run，没有换 mode 请求。
- `routers/runs.py:114–117` 只检查 plan override 的键，不校验值。因此 mode 合同检查放在覆盖之后，集中在现有 services/runs 边界供 API、preflight、render 复用。只补相关 profile/strategy 合法配对及批次合同；这不是对所有历史 plan 数值覆盖漏洞的完整修复。
- `pipelines/run.py:93–116` preflight 在镜像阶段之前，新增纯校验要先于外部工作；render 再检查覆盖自动 retry。`render/train.py:112–125` params 白名单继续拒绝 mode/Hydra 键，plan_overrides 也不得开放 mode 或任意 Hydra 注入。
- `frontend/src/pages/Runs.tsx:173–204,280–290,380–406,559–573` 已有统一 body、预设、review、费用确认和详情配置；独立 typed mode state 能避免被预设重置。`frontend/src/lib/api.ts:590,631,674` 扩 DTO 即可；不往 PlanCard/资源 Plan 塞模式，不改节点、价格或 CSV。
- 需补 `backend/tests/test_runs.py`；现有 `_hydra:17` 只是拆分 override 字符串，**不是 Hydra compose**。复用固定合同/LoRA、RayJob、自动 retry、resume 测试，但必须另做锁定 verl 的真实离线 compose。

## 5. 验证、镜像与云测计划依据

### 分层门禁（均未执行）

1. hermetic 控制面：临时 SQLite、拒绝真实 AWS/K8s 的 factories（`backend/tests/conftest.py:24,70`）；模式请求/持久化/重读/ConfigMap、旧记录、非法值/字典绕过、批次、费用取消、budget/deadline/guardian/auth/IRSA 回归。模拟无 step 但持续运行，配置有限预算/deadline，验证现有停止/收尾路径；不声称有新的无进展检测器。
2. 固定框架：两模式×四策略的 override 检查；`ppo_trainer`、`ppo_megatron_trainer` 真实 Hydra compose。CPU Toolkit 测试参考 `T/tests/backends/verl/test_trainer_batching.py:213–238`，核 registry/MRO、native 归一化、分区/padding、批次拒绝及失败/恢复边界。只在已备好锁定依赖的隔离私有副本运行，不能在共享缓存安装/写入；缺依赖 skip 不算通过。
3. 项目完整 `make verify`，加严格 i18n；loopback mock 浏览器中英、390px、键盘、preview/create payload、旧详情/刷新、预设与错误重试、费用取消。无新增前端测试框架。
4. 云端另行授权：配置字符串不足以证明实际 trainer。保存实际类、ReplayBufferAsync、FullyAsyncLLMServerClient、同池 GPU 布局与 abort/sleep/actor update/权重更新/resume 的运行时证据。若默认日志不足，使用另行审阅的最小临时诊断，不能仅凭 UI 标签、`rollout.mode=async` 或推断判通过。

### 现成镜像与真实用例

`backend/app/pipelines/trainer.py:37–71` 按 region/profile/toolkit SHA 选镜像；`trainer_image/Dockerfile:28–45` 用 toolkit frozen lock 安装并 import trainer 模块。同 SHA/相应 profile 镜像原则上含 async 类，**不需为 mode 单独构建镜像**。本轮未查 ECR：获批时核 ledger、ECR digest、实际 Toolkit/verl/TQ 版本及注册类；不匹配按既有显式 build 流程处理，构建费用也须获批，不自动升级 main 或重建现有运行时。

候选最小真实训练：Qwen/Qwen3.5-2B、GSM8K AgentCore runtime/数据、FSDP full、单节点 p5.4xlarge（1×H100 80GiB，`backend/app/catalog/instances.py:43–46`），GPU head 训练、CPU submitter 在 system 池。p5.48xlarge 是 8 卡，不能静默替代。B=m=8、n=4、2 步、save_freq=1、关闭基线验证的依据为 `T/experiments/qwen35_2b_gsm8k/trainer/train_qwen35_2b.sh:116–126` 的 **sync smoke**，不是 async 已验结果；数据量、轨迹长度、区域容量、镜像与预算须在执行时核实。

范围收敛后，真实验收只要求一个最小 colocate_async 用例，双模式兼容由离线回归保护；额外付费的同步对照、长短轨迹、失败注入、重启试验不在本次必做清单。普通 smoke 不要求随机出现非零 staleness；未做专门试验就不能宣称跨版本续写、故障恢复或吞吐对照已验收。保存实际类/调度证据、有效 token/reward、非 padding 行、真实 optimizer 更新与 checkpoint 内容（FSDP 核优化器状态），不能只凭 step 日志或目录。TQ 0.1.8 不恢复队列及 actor/data 恢复边界如实说明。

云测授权须重新列明：版本、区域、provider/容量类型、机型/节点组、测试 runtime/数据、总预算与停止新增资源线、容量等待/总时长/首次及后续无进展上限、重试数、清理范围。旧 US$100 等预算不沿用；历史容量失败不是当前事实。终态核 Job、RayJob/Pod、节点组 desired/实际计费实例、测试 Runtime/session，保留/删除边界先约定，不能只记“发出删除”。失败停止并清理，不因验收项未达成擅自追加 GPU 尝试。

当前没有必须升级依赖才能接入两模式的证据；最新设计仍待批准，真实训练与四策略硬件可用性均未验证。

## 6. 独立审阅后的安全收敛

独立审阅指出：仅提示设置 max_hours/budget 不能称作有界保护。现有 `routers/runs.py:38–39` 允许二者为空，`services/runs.py:91–104` 将小时截为整数秒，`render/train.py:525–526` 仅在值为真时写 deadline；极小正数小时也可能截成0后遗漏 deadline。guardian 对忙碌 Pod 不按空闲停止（`cluster_assets/guardian.py:63–69`），不能替代异步无进展保护。

因此待审批设计要求：仅 colocate_async 必须给有限 max_hours 且 `int(max_hours*3600)>=1`，API/preflight/render 共用校验，最终 manifest 必有正整数 activeDeadlineSeconds；同步和旧缺模式记录的原行为不变。现有前端 compute 默认 max_hours=6（`frontend/src/pages/Runs.tsx:139–141`）可复用，不额外增加字段，清空后需用户补值而非自动扩大时长。该限制按 attempt 生效，排队/容量等待/重试及释放尾段仍须分别说明，不能包装成整个 Run 或账单硬上限。

真实验收收敛为用户要求的一个最小 async 用例；额外 GPU 故障注入、重启和同步吞吐对照不列必做项。离线回归仍覆盖两模式及现有策略合同；实际云测 max_retries=0，并在新的机型/区域/预算批准后执行。这些改动只更新规划工件，不是已实施或已运行的测试。
