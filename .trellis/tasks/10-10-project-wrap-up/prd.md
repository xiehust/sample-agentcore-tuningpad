# 项目收尾：工程缺口、验收与记录同步

## Goal

汇总已确认工程缺口、真实环境验收与过期记录；区分本地修补、跨仓库工作和需单独批准的云端操作。

## 授权与当前阶段

- 2026-10-10：用户先同意建立收尾任务，随后确认首批范围为“TuningPad 的错误提示本地化和记录同步”；暂不修改 toolkit、不做云端操作。
- 首批只覆盖 R1a、R4a、R4b，由本任务直接承载；其余需求仍为后续跟踪，不因首批完成而关闭。首批设计和执行计划见 `design.md`、`implement.md`。
- 用户已最终确认首批方案，并要求改用 Playwright CLI 验收。首批 B1–B4 已完成。随后用户明确要求 commit/push 并继续其余事项；首批提交 `8889a98` 已推送至 `origin/main`。
- 整个跟踪任务保持 `in_progress`。R1b 的单文件测试隔离修复已通过验收并推送 toolkit 分支 `fix/rollout-test-isolation`（`99ab5d7`），尚未合入 main；证据见 `../10-10-toolkit-test-isolation/verification.md`。真实云端操作仍需单独批准费用；其他缺口见 `remaining-evidence.md`。
- 原任务 `../10-10-rollout-failure-policy/prd.md` 继续拥有失败策略的 AC1–AC5；本任务引用其结果，不重复执行或提前归档。

## Requirements

### R1 本地工程缺口

- **R1a 错误提示本地化**：补齐 `run.rollout_failure_guard` 的中英文说明，并核对实际 JobPanel 展示路径，而不只添加未被使用的 locale key。审计时证据：`backend/app/pipelines/run.py:425`、`frontend/src/components/JobPanel.tsx:129`；当时两种 `common.json` 均缺少该 key。依据 `.trellis/spec/backend/error-handling.md`，新增用户可见错误码须有翻译。首批已补齐并在真实组件的本地 mock 场景验证，详见 `verification.md`。
- **R1b toolkit 测试隔离（修复分支已验证，待合并）**：`tests/test_rollout_entrypoint.py` 已在 app 构造前 mock S3，并等待和断言后台保存。75 个相关测试通过，Python/AWS 验证器记录为零违规；生产代码和依赖未改。提交 `99ab5d7` 已推送 toolkit 独立分支，但主分支尚未包含修复；详见子任务验收记录。

### R2 当前功能的部署与真实验收

- **R2a Rollout failure policy AC5**：原任务 AC1–AC4 已勾选，AC5 未完成。需单独批准后，以小规模训练注入部分 agent 异常，确认异常样本被丢弃、同组其他样本继续训练、health 图有指标且 step 完成。证据：`../10-10-rollout-failure-policy/prd.md:60-64`。未经真实验收，不能据离线测试声称该行为已在 Ray/GPU 环境成立。
- **R2b OfficeBench 镜像更新**：最后日志仍要求重建 agent 镜像，使空回复 `IndexError` 修复生效。agent 镜像不随 toolkit revision 自动重建；本次未查询线上镜像。先确认当前镜像是否已包含修复，再决定是否请求 CodeBuild/部署授权，不能盲目重复构建。证据：原任务 PRD 的 R7、`.trellis/workspace/river/journal-1.md:153`。

### R3 历史验证缺口（先核实证据，不直接当作功能缺陷）

- **R3a Megatron V4**：最新日志记为等待 p5 容量，尚未找到关闭记录（`.trellis/workspace/river/journal-1.md:155`）。核对该验证的模型、配置和完成标准后，再决定是否补跑。
- **R3b OfficeBench Qwen3.5-4B FSDP LoRA（部分验证）**：本地 ledger 已查到 `run-210c57c0cb` 三步训练、step 3 导出及对应评测链；评测样本仍有失败，且不是完整两轮训练预设。详见 `remaining-evidence.md`。因此保持 `templates/officebench/template.yaml:30-35` 的“未验证”标签，不重复声称完全没有训练实测。
- **R3c HyperPod 原生 GPU 实例组**：平台 PRD 要求该路径验收，但现有实测主要为 EKS 托管 EC2 节点组。证据：`../archive/2026-10/10-04-agentic-rl-platform/prd.md:124-127,135-141`。两种 provider 不应混为同一路径；优先查找遗漏的历史证据。
- **R3d ADOT 冷启动影响**：可观测性要求训练冷启动不变，但镜像增大对训练 runtime 拉取时间的影响未单独测量。证据：`../archive/2026-10/10-09-eval-observability/prd.md:63`、`design.md:126-127`。这是测量缺口，不代表已确认性能退化。
- **R3e 自动恢复（部分验证）**：`data/jobs/job-d7184ccc23.log:17-34` 和 ledger 证明真实发生过失败重提、后端重启后恢复 monitor、最终完成；尚不能证明 trainer 已从已有 checkpoint 加载权重后续训。详见 `remaining-evidence.md`；不以 guardian 缩容或重提请求替代 checkpoint 恢复证据。

### R4 记录、交付与资源善后

- **R4a 任务状态**：审计时 `../10-10-rollout-failure-policy/task.json:6` 仍为 `planning`，与已交付代码及 AC1–AC4 不一致。首批已改为 `in_progress` 并补充说明；AC5 仍未完成，`completedAt` 仍为 null，没有归档。
- **R4b 过期记录**：把下文已结案项目与当前待办区分清楚；保留历史日志的时点语义，用后续说明替代将历史改写为当下事实。
- **R4c 提交发布（首批已完成）**：用户随后明确授权 commit/push。已 fetch 并确认远端无分叉，提交 `8889a98`，连同此前本地未推送历史成功推送至 `origin/main`；推送后核对 ahead/behind 均为 0。后续材料按用户本次发布授权提交。
- **R4d 云资源善后**：旧日志中的 `rl-dev-2` 保留/删除决策尚未找到结案证据（`.trellis/workspace/river/journal-1.md:110`）。本次没有查询 AWS，不能声称资源仍存在或仍计费。确认现状、费用和保留意图后再决定操作；本任务不授权删除资源。

## Acceptance Criteria

- [x] AC1（R1a）：中英文界面都能显示失败保护的本地化说明；技术诊断信息不丢失；相关回归和项目质量门禁通过。
- [x] AC2（R1b）：跨仓库测试隔离有修复及离线验证证据，或用户明确决定移交/延期并留下对应记录；不能仅以测试返回成功认定没有后台 S3 访问。
- [ ] AC3（R2a）：原任务 AC5 有真实验收证据，或用户明确接受将它延期；任何延期均不得标为“真实验收通过”。
- [ ] AC4（R2b）：记录实际使用的 OfficeBench 镜像所含修复版本与安全验证结果，或用户明确接受延期；已有新版镜像时不重复构建。
- [ ] AC5（R3）：每个验证缺口都有可复查的运行证据、仍待验证的范围，或用户确认的延期决定；评测、训练和不同 provider 的证据不得互相替代。
- [ ] AC6（R4）：任务状态与真实交付状态一致，过期待办已澄清；发布与云资源处置有明确决定或被标为待决定，不执行未经授权的 push、构建、扩容或删除。

## 已结案项目与实施前审计基线

以下是 2026-10-10 实施前本地检查结果；首批完成后的验证见 `verification.md`，后续操作仍需重读实时状态：

- toolkit 失败策略已由 PR #2 合并到 `main`（`642471f`，包含 `2788cd3`）；“待 push/PR”是过期记录。
- guardian 脱离后端自动缩容已在 2026-10-09 实测：`.trellis/spec/backend/aws-k8s-operations.md:92-94`。
- Eval 调用链和瀑布流 AC4 已真实验收并归档：`../archive/2026-10/10-09-eval-observability/prd.md:78-80`。这不关闭 R3d 的冷启动测量缺口。
- 导出模型评测差异已由权重 A/B 解释，EFA 实际使用已有后续日志证据：`../archive/2026-10/10-04-agentic-rl-platform/prd.md:146-154,175-184`。
- 本地 `make verify` 与 `python3 scripts/i18n_check.py --strict` 均通过；没有修改产品代码，也没有运行真实 AWS/K8s 操作。严格 i18n 通过不证明 R1a 的后台 job 错误已经本地化。

## 首批验收（R1a、R4a、R4b）

- [x] B1：JobPanel 展示当前语言的错误说明，保留原始错误码与后端诊断原文；无翻译或无错误码时仍显示原文且不重复；无错误的 job 不显示错误告警。
- [x] B2：为 `run.rollout_failure_guard` 增加中英文翻译，明确停止训练的原因和不自动重试；切换语言后说明更新，原始诊断不丢失。
- [x] B3：失败策略原任务状态改为 `in_progress` 并注明“实现完成，待 AC5 真实验收”；更新 toolkit 合并事实，保留 AC5 未勾选、`completedAt=null`。历史日志以追加说明澄清，不改写旧会话。
- [x] B4：mock-only 浏览器检查覆盖 en/zh-CN、未知/空错误码、相同文案不重复、无错误状态和窄屏长诊断；`make verify` 与严格 i18n 检查通过。无真实 AWS/K8s 操作，无新增项目依赖；用户批准的 Playwright CLI/skill 安装在用户级目录。

## Out of Scope

- 首批实现不修改 sibling toolkit，不执行 AC5 或其他真实验收，不重建/部署镜像，不决定云资源去留，不归档仍有验收缺口的任务。首批实现时的“不提交/push”限制已被用户后续的明确发布授权取代。
- 不启动应用连接真实账号，不执行 CloudFormation、CodeBuild、GPU 扩容、镜像部署、资源删除或其他云端操作。
- 不修改后台错误码、训练失败保护、重试逻辑、API/数据库契约、鉴权或费用确认；不扩大到新训练功能、异步 rollout failure policy 或控制台新增配置项。
- 不全面补译所有历史后台错误码；只复用已有错误本地化能力，并新增本次遗漏的 key。
