# 首批收尾设计：本地化与记录同步

## 边界与当前行为

- 目标是修复后台 job 错误的显示层，不改变训练失败判定或恢复策略。
- `lib/api.ts:localizedMessage(code, fallback)` 已为非 2xx API 错误提供 `apiErrors.<code>` 翻译和原文回退。job 查询成功返回的 `Job.error/error_code` 不走这个异常分支。
- `components/JobPanel.tsx` 直接展示 `job.error`，因此只添加 locale key 不会改变页面。
- `Job.error` 和 `Job.error_code` 均可为 null；当前组件仅在 `job.error` 非空时展示错误。保留这个条件，不为没有错误的 job 人为生成告警。
- 本次仅新增 `run.rollout_failure_guard` 翻译；不借机补齐所有历史错误码，不改 `useJob` 的轮询/状态/回调，不改 API DTO。

## 最小方案与兼容性

1. JobPanel 导入并复用 `localizedMessage`，在渲染时由错误码计算展示说明。继续使用现有 `useTranslation`，切换语言触发重新渲染，不把翻译存入只计算一次的 state。
2. 告警主行保留错误码并显示本地化说明。当说明与 `job.error` 不同时，在同一告警内的下一行保留原始诊断；相同时只展示一次。未知或空错误码回退原文，不显示缺失翻译 key。
3. 原始诊断仍由 React 文本渲染，不使用 HTML 注入；长错误码和诊断可换行，不强制截断，不引入折叠状态或新的交互控件。
4. 在两个 `common.json` 的 `apiErrors` 下按现有字面点分 key 布局增加译文。说明训练因大量 rollout 被丢弃而停止、不会自动重试，并提示先检查 agent/训练日志。保留现有手动重试按钮，不把显示修补变成行为变更。
5. 原失败策略任务改为 `in_progress`，notes 说明实现完成但 AC5 待批准/验收；`completedAt` 仍为 null。PRD 更新 toolkit 合并事实并标明旧方案描述的历史时点；AC5 保持未勾选。
6. 日志通过追加本次收尾说明澄清已合并分支、guardian 已验证等过期 Next Steps，不重写历史会话。新增证据与以前的验证结果明确分开。

复用现有 helper 比在 UI 再实现一套 `i18n.exists`/fallback 更小；在渲染层追加说明比替换持久化的 `job.error` 更能保留诊断价值。

## 文件范围、验证与回退

预期修改范围：

- `frontend/src/components/JobPanel.tsx`：接通已有本地化 helper，保留错误码和诊断原文。
- `frontend/src/locales/{en,zh-CN}/common.json`：增加失败保护说明。
- `.trellis/tasks/10-10-rollout-failure-policy/{task.json,prd.md}`：同步实际交付状态，保留真实验收缺口。
- `.trellis/tasks/10-10-project-wrap-up/`：记录本批验收，不关闭剩余需求。
- `.trellis/workspace/river/journal-1.md` 及其索引：仅在收尾时追加当前会话和对应索引。若确有新约定，遵循 finish-work 流程做最小 spec 更新，不扩大产品修改范围。
- mock QA 脚本/截图只放 gitignored 的本地 QA 目录；不添加前端测试框架依赖。

验证遵循 `implement.md`。前端只能指向 loopback mock API，明确设置 `TUNINGPAD_API`，不能使用默认真实后端代理。没有后台或数据库迁移；回退仅还原本批 UI/locale 差异，不撤回此前代码，也不把未完成验收写成完成。
