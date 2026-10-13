# 实施顺序与验收门禁（本地完成，真实云测待授权）

本地证据与中断后核验见 verification.md；最终父代理 make verify 已通过。下一步是 cloud-test-plan.md 的独立费用/资源批准，不能据此自动开始 GPU 作业。

前置已满足：用户在最新最终方案后回复“ok”，批准代码实现与本地验证。可启动本任务并按下列顺序实施；结果写入 verification.md，未执行不记通过。真实云测仍需独立的新预算/配置授权，旧预算不沿用；代码验证不调用真实 AWS/K8s，不启动云构建/部署/训练，也不重启现有服务。本地完成后仅为 cloud-test-plan.md 单独读取了 Pricing/ECR（见 verification.md），没有资源写操作。

## 1. 按顺序实施

- [x] 复读本任务三份核心稿、research 与两个 JSONL 指向的真实 spec；记录现有脏文件基线，保护他人改动。确认固定 SHA/锁定依赖不漂移，CPU 验证环境可用；缺环境报告阻塞，不安装到源码缓存。
- [x] 后端：在纯 `render/train.py` 定义共享模式/alias/批次合同，`services/runs.py` 组合 deadline 校验（避免循环 import）；`routers/runs.py` 接 preview/create/新 spec；`render/train.py` 接明确 mode、映射与固定异步 overrides、批次正整数/整除。`pipelines/run.py` 接早期 preflight，render 覆盖所有提交/重试。无需 ORM/planner 迁移；新错误两种 locale 同步。
- [x] 前端：`lib/api.ts` 请求/preview/可选旧 spec 类型；`pages/Runs.tsx` 独立 mode state、模型选择、review/费用确认/详情。用 V2 与 t()，落实实验性、sync-only guard 与恢复限制提示，不改预设/LoRA 策略。
- [x] 扩 `backend/tests/test_runs.py`；必要时补 `test_engine.py` 的重启断言。完成下列离线矩阵，审查全部差异与成本/安全护栏，而非只核 happy path。

## 2. 离线验证矩阵

| 门禁 | 必留证据 |
| --- | --- |
| API/存储 | 省略/显式 sync/async 的 preview→create→重开 DB→详情；旧 spec 缺键仍 sync 且不回写；null/空串/未知/错误类型/separate_async 4xx，无新 Run/Job；两个字典 mode/Hydra 注入拒绝；最终 profile/strategy 冲突拒绝。 |
| 提交/恢复 | 首次、自动 retry、后台重启、手动 resume 使用同模式与 ConfigMap；不重复提交；无改 mode 入口；非法持久值在镜像/扩容前失败，render 再拒绝。 |
| 固定配置 | 两模式×四策略（FSDP/Megatron、full/LoRA）；实际 Hydra compose `ppo_trainer`/`ppo_megatron_trainer`，正确 alias/native 子树与 `+` 键；保持 FSDP safetensors/Megatron merge、动态批次/固定微批=1、IS/registered sessions。 |
| SDK CPU 合同 | 真实 registry/MRO 与 native 归一化；V1/actor-only/loss/sync-step 限制；B/m 分区、扩行与 padding，正数/整除及 0/负/小数/bool/string；省略/null mini 与其他合法零值不回归；sync guard 存在而 async 不假装存在；TQ 0.1.8 队列恢复门关闭。 |
| 安全/收尾 | 保留费用确认；未确认不启动。异步 max_hours 缺失/null/非有限/转换后不足1秒，在API、preflight和render拒绝且无新Run/Job/外部工作；合法异步RayJob确有正整数deadline。同步缺失时长不回归。budget、guardian、取消/失败/retry、auth/IRSA/SG保持；stub无step场景验证现有停止路径，但不冒充K8s真实超时。 |

计划命令（产品变更后执行）：

```bash
(cd backend && uv run pytest tests/test_runs.py tests/test_engine.py -q)
(cd frontend && npm run lint && npm run typecheck)
python3 scripts/i18n_check.py --strict
make verify
```

锁定框架检查在已备依赖的**隔离私有副本**运行：`uv run --offline --no-sync pytest tests/backends/verl -q`，并执行生成 override 的真实 Hydra compose；不得从控制面常规测试拉取/安装 GPU 依赖。记录版本/hash、实际测试数与 skip；缺 verl/CPU 依赖或 compose 未跑标 BLOCKED，不能把 `_hydra` 字符串断言/skip 当通过，不修改 Toolkit 缓存。

浏览器用 `TUNINGPAD_API` 指向 loopback mock，不启动真实账户后台：en/zh-CN、390px/键盘、默认与两模式、preview/create payload、保存后刷新/旧详情、预设/模板/错误重试不丢选择、风险文案与费用取消不发 create。保留截图/请求证据；build 不替代浏览器验收，不新增框架依赖。

## 3. 独立云测申请与执行计划

- [ ] 先提交一次完整申请：区域/provider/容量/机型/节点组、runtime/数据、镜像 digest/版本、预算与停止新增线、容量等待/总时长/首次和后续无进展上限、重试/清理范围。运行中不擅自换机型/区域或追加预算，历史 Spot/OD 失败不代表当前容量。
- [ ] 获批后核同 SHA/profile 镜像及实际 Toolkit/verl/TQ/registry，优先复用；缺镜像走已有受批准的构建流程。候选 Qwen3.5-2B/GSM8K/FSDP full、1×p5.4xlarge、B=m=8、n=4、2 步每步 checkpoint，仅是固定 sync recipe 的起点；执行时核数据、内存和容量，不承诺可用。
- [ ] 获批后只做一个最小 `colocate_async` 真实训练用例，必须有有限时长/预算，测试 max_retries=0；留类、ReplayBufferAsync、FullyAsyncLLMServerClient、同池 GPU、abort/sleep→actor update→权重同步/resume 证据。不足时先审阅最小临时诊断，不以标签或进程存活代替。
- [ ] 核有效 token/reward、非 padding 行、optimizer 更新与 checkpoint 实际内容。同步行为以离线回归保护；本次不强制额外付费的同步对照、失败注入、长短轨迹或重启试验。保留固定版本的 actor/data/TQ 恢复限制说明，未做部分明确未验收，不承诺完整恢复或提速。
- [ ] 到停止线立即停止并清理；核 Job/RayJob/Pod 终态、节点目标与实际计费实例、测试 Runtime/session，明确保留资源。失败也必须收尾，不只保存删除请求。

## 4. 完成与回滚检查

报告分别列：hermetic、固定框架、make verify、浏览器、GPU 运行时/训练/清理，每项 PASS/FAIL/BLOCKED 与证据；无云授权就明确真实验收待办，不宣称全部完成。高风险处是旧 spec fallback、profile override 后校验、Hydra `+`、失败/恢复误导；撤回先停 async 新建与已有作业，再按 design 安全回滚，不修改历史模式或借机升级依赖。
