# 控制台支持 verl 同卡异步训练

状态：用户在最新最终方案（含异步强制有效时限、保持旧同步、一个最小真实异步用例且云测另批）后回复“ok”，已批准代码实现与本地验证。用户随后批准独立US$20云测及单卡CPU资源修正。唯一一次p5.4xlarge OD实测请求因容量不足停止，资源清理已验证；AC5真实训练仍BLOCKED，不授权自动重试。详见cloud-test-report.md。旧云测预算不适用。

## 目标与价值

用户无需改脚本即可选择真正的 trainer 调度模式，预览、保存及查看 Run 时得到一致结果；已有同步工作流不被改变。不以现有 vLLM `rollout.mode=async` 标签冒充异步训练。

## 已确认事实

依据 [research.md](research.md) 的三报告汇总与源码抽查：固定 Toolkit **7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6** 对应 **verl 0.9.0 / TransferQueue 0.1.8**，已注册两种 trainer。同池异步为同一 GPU 池分时采样/训练，可续写部分轨迹，并非 GPU 内核同时跑。SDK 失败 guard 只用于同步；异步失败整组淘汰补采。固定 TQ 不支持队列 checkpoint 恢复；这些是确定限制，不是待猜测的能力。

## 需求

- **R1 选择与兼容**：只提供 sync / colocate_async，默认及旧 Run 缺模式均为 sync。模式从 UI→preview/create→持久化→提交→详情一致；重试、后台重启与续训不换模式。显式非法值拒绝，不能默默当同步。
- **R2 最小安全接入**：遵守固定 SDK 的批次、权重同步和配置约束，非法组合在镜像/扩容前阻止；保留合法同步行为、现有 FSDP/Megatron 与 full/LoRA 策略，不借机重写参数系统或改变资源数量。
- **R3 可理解的差异**：双语选择、review、费用确认及详情显示模式；同池异步按实验性能力提示无 sync drop-fraction 熔断、可能持续补采、恢复不完整及无提速保证。保留现有 checkpoint/续训入口，不承诺在途样本或所有 profile 的优化器状态连续性。
- **R4 成本与安全**：保持预算、guardian、GPU 费用确认、认证/网络边界、registered sessions、IRSA。新增 colocate_async 必须显式设置有限 `compute.max_hours`，且能生成至少 1 秒的正整数 RayJob deadline；缺失/null、非有限值或转换后为0均须在写入/镜像/扩容前拒绝，render 再校验。同步及旧 Run 的可空时长语义不变。该时限按 RayJob attempt 生效，不冒称覆盖排队、容量等待、全部重试或账单硬上限；不新增 watchdog 平台。
- **R5 证据与授权**：固定依赖不追 main；现成镜像复用与真实用例只能作为执行时核验的计划。代码、浏览器、运行时模式、真实训练/恢复/清理分别验收；云测须新配置、新预算批准，旧预算不适用。

## 验收标准

- [x] **AC1（R1）**：双模式 API 往返与旧 spec 缺键测试通过；持久化不回填旧记录，所有提交/重试模式一致；非法/null/未知模式及字典绕过无新 Run/Job 副作用。
- [x] **AC2（R2）**：固定 verl 两种配置文件实际 Hydra compose、两模式×四策略渲染与 SDK 批次/registry 合同测试通过；保留 LoRA 特例，不能只靠 override 字符串断言或依赖缺失 skip。
- [x] **AC3（R1、R3）**：mock 浏览器中英、窄屏/键盘验证默认、选择、预览 payload、保存后详情/刷新、旧记录、预设/模板与错误重试；风险提示准确，取消费用确认不提交。
- [x] **AC4（R2、R4）**：hermetic pytest 及成本/安全/取消收尾回归、严格 i18n 和 `make verify` 通过；不接真实 AWS/K8s。异步缺失/null/非有限/不足1秒时长均被拒绝且无写入或扩容，合法异步 manifest 确有正整数 activeDeadlineSeconds；同步缺失时长仍保持原行为。
- [ ] **AC5（R3、R5；单独授权）**：完成一个最小 colocate_async 真实训练用例，取得实际 trainer/异步 buffer/client、同池暂停/训练/权重更新/恢复生成证据，以及有效轨迹、非 padding 训练行、optimizer 更新和真实 checkpoint；不能仅凭 UI、rollout 标签、step 日志或目录判通过。不要求额外付费的同步对照、故障注入、长短轨迹/重启实验，也不把未做的恢复或吞吐对照宣称已验证。
- [x] **AC6（R4、R5；独立授权后容量失败，清理已验证）**：受批准停止线约束，核实 Job/RayJob/Pod、节点目标/实际计费实例和测试 Runtime/session 收尾；未获云授权或未跑项目单列未验收，不能称完整验收。

## 不在范围

separate_async、多资源池、独立采样基础设施、高级异步旋钮、新 SDK guard/队列恢复实现、自动依赖升级、性能/质量承诺、四策略全部 GPU 认证，以及改动已有 profile/LoRA 支持政策。技术决策见 [design.md](design.md)，执行顺序与命令见 [implement.md](implement.md)。
