# 首批验收记录 — 2026-10-10

## 已验证行为

使用实际 `JobPanel`、`useJob`、`lib/api.ts` 与 locale 文件，渲染到独立 QA 页面；只替换 API 服务响应，没有替换组件实现。

| 配置 | 结果 |
| --- | --- |
| en，1280 px | PASS |
| zh-CN，1280 px | PASS |
| zh-CN，390 px | PASS |
| en，390 px | PASS |

上述配置均断言：失败保护翻译与错误码可见、原始诊断及换行保留、已有其他翻译可用、未知/null/空错误码回退不重复、相同译文不重复、null/空错误无告警、长错误码及诊断完整且不横向溢出。通过页面按钮在同一批 job 上切换语言；mock 请求记录中只有 job fixture GET，没有触发重试或其他变更。

已查看英文桌面与中文窄屏截图，说明和诊断分层可读，没有布局溢出。独立只读代码审阅未发现有证据的缺陷。原失败策略 task.json 为 `in_progress`、`completedAt=null`，AC5 仍未勾选。

## 命令与证据

- `cd frontend && npm run lint && npm run typecheck`：PASS。
- `make verify`：PASS；在 Playwright 验收期间再次完整执行。
- `python3 scripts/i18n_check.py --strict`：PASS。动态错误码出现在静态 unused-key 报告中，仅为报告项；浏览器已验证实际使用。
- `git diff --check`：PASS。
- Playwright CLI 0.1.22 通过 `attach --cdp=http://127.0.0.1:9237` 连接本次独立 Chromium，页面仅为 `http://127.0.0.1:5197/qa.html`。CLI 工作目录为 `.run`，所有自动产物均被 Git 忽略。

本地复验入口（从仓库根目录启动服务，CLI 命令在 `.run` 下执行）：

```bash
TUNINGPAD_API=http://127.0.0.1:8197 ./frontend/node_modules/.bin/vite --config .run/job-panel-qa.config.mts
playwright-cli -s=tp-wrapup attach --cdp=http://127.0.0.1:9237
playwright-cli -s=tp-wrapup goto http://127.0.0.1:5197/qa.html
playwright-cli -s=tp-wrapup eval "$(cat job-panel-qa-check.js)"
```

QA 服务拦截所有 `/api/` 请求，仅允许 fixture GET；未匹配或非 GET 请求直接拒绝，不转发到真实后端。CDP 使用独立测试 profile，不复用用户浏览器。

- Harness：`.run/job-panel-qa.html`、`job-panel-qa.tsx`、`job-panel-qa.config.ts`、`job-panel-qa.config.mts`。
- 断言：`.run/job-panel-qa-check.js`。
- 结果与截图：`.run/job-panel-qa-{en,zh}-{desktop,narrow}.{json,png}`。

中途排除的是 QA 工具问题：agent-browser 启动器卡住后停止；按用户要求换用 Playwright CLI。Vite 实验性 runner 不适合 React 插件的延迟加载，QA 改用 ESM 打包配置并从 `frontend` 加载已有插件；这些改动仅在 gitignored harness 内。

## 范围与未完成事项

- 已完成的是首批 R1a/R4a/R4b、B1–B4；只将总体验收 AC1 标为完成。
- 原失败策略 AC5、OfficeBench 镜像更新、toolkit 测试隔离、其他历史真实验收、发布与资源善后均未执行或结案。
- Playwright CLI 与 skill 的用户级安装由用户明确批准，不改变项目 package.json/lockfile；没有下载新浏览器。
- 没有 AWS/K8s 操作，没有提交、push 或任务归档。质量门禁中的第三方弃用警告没有在本批扩大处理。
- 未运行会自动提交的 Trellis 收尾脚本；会话记录与索引手工追加，等待用户决定提交。
- 验收后已 detach Playwright 会话并停止本次 mock Vite 与独立 Chromium；确认 5197/9237 不再监听，未停止其他会话或服务。
