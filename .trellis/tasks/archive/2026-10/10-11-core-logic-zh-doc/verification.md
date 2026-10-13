# HTML 收尾验证记录

## 本节点范围与结论

- 交付：`docs/core-logic.zh-CN.html`；源码 HEAD 为 `f7c2b94aa91d8281ae6c03088fe6e212daf3a160`。
- 本节点接收独立 source-review，分段通读 HTML，核对所涉权限及账本写入源码，只改一处事实措辞；新增本记录。不改业务、依赖、README、其他 HTML、任务状态或共享 spec，不归档、不提交。
- 已通过下述本地静态检查。**本节点未进行浏览器验收、未运行 `make verify`，不能宣布完整任务验收通过。** 没有安装依赖、启动服务、调用 AWS/K8s 或读取 sibling toolkit、真实配置及运行数据。
- 11 章覆盖：系统总览、启动与模块边界、数据模型与台账、Job 引擎与状态、准备分支、规划与参数、Run 完整链路、前端反馈、失败恢复与清理、安全费用与测试、源码地图与术语。两组架构/训练时序图共 5 个 SVG。

## 独立 source-review 与最小修正

独立审阅者报告：全文阅读、核心链路源码抽查与全量引用结构检查，**无阻塞性核心事实错误；一项非阻塞措辞问题**。这不是全部段落的逐句形式化证明，也不是云端或浏览器实测。

- 问题：§10.2 / HTML 第 863 行的“guardian 写操作限集群/节点组”范围过宽。
- 本节点直接核对 `backend/app/services/components.py:158–203`：SageMaker 更新指向指定 HyperPod ARN，EKS 更新指向指定集群的 nodegroup；同时允许平台 bucket 的 `clusters/*` 上 `s3:PutObject`，以及限定 exec role、`iam:PassedToService=sagemaker.amazonaws.com` 的 `iam:PassRole`。`cluster_assets/guardian.py:165–182` 对应实际缩零和 S3 账本写入。
- 唯一正文修正：“guardian 的缩容操作限定到指定集群/节点组；另有 S3 账本写入及受限 PassRole 权限”。原有 IRSA 源码引用 `components.py:114–238` 已覆盖上述证据，无需增改引用或业务代码。
- 独立审阅确认的重点包括：Job retry / Run resume / attempt 区别，queued cancel 绕过 hook，reconciler 未注册，deadline 可空且按 attempt 生效，FSx checkpoint 与 S3 索引/显式 export 区分，独立后处理 pipeline，前端轮询/defaults，图上请求回连方向，安全与外部审计边界。其抽查覆盖 engine、main/models/db、run、planner/render、serving、guardian，并补查准备、集群删除、鉴权及测试源码。
- 本节点没有将上述报告包装成再次独立完成的全链路审计；仅对修正点直接核证，其余正文保持原样。

## 本地只读检查：命令与实测结果

从仓库根目录执行；以下命令均已实际完成，退出码为 0。

| 命令 / 工具 | 检查与实测结果 |
| --- | --- |
| `git rev-parse HEAD` | 与 HTML 快照一致，完整 SHA 见上。 |
| `sha256sum docs/core-logic.zh-CN.html core-architecture.zh-CN.html` | 修正前目标与收到的独立审阅哈希一致；其他 HTML 基线一致。 |
| `python3 -B -`（一次性 stdin heredoc） | 仅 Python 标准库 `HTMLParser` / `pathlib` / `urllib.parse` / `ElementTree` / `re` / `hashlib`；不导入 app、不请求网络、不写检查脚本或缓存。完整 heredoc 在本节点工具执行记录中，结果逐项列于下表。 |
| `git diff --check && git diff --stat && git status --short` | 退出 0，tracked diff 为空；未跟踪项为本任务目录、既有 `core-architecture.zh-CN.html`、`docs/`。该 Git diff 不覆盖未跟踪 HTML，因此另用全文件解析及哈希校验。 |

一次性 Python 检查实测：

| 范围 | 结果 |
| --- | --- |
| 元数据 / 语义 | 严格 UTF-8 解码、无替换字符；HTML5 doctype、`lang=zh-CN`、UTF-8 charset、viewport、页面 title 正确；单一 head/body/nav/main/article/h1，显式标签闭合。 |
| ID / 导航 | 44 个唯一 ID；30 个本地 href、23 个 ARIA 引用、24 个 SVG URL 引用均有目标。11 章目录顺序与设计一致，另有返回顶部，共 12 个 nav 条目。 |
| 全量 source-ref | 254 处引用、70 个文件；全部是相对文件路径，无 scheme/netloc/query/fragment；resolve 后位于本仓库且文件存在。 |
| 允许源码范围 | 仅 `backend/app/`、`backend/tests/`、`frontend/src/`、`scripts/`、`cluster_assets/`、`trainer_image/`、`templates/`；另精确允许 `Makefile`、`start.py`、`stop.sh`、`backend/pyproject.toml`、`frontend/package.json`、`config/tuningpad.example.yaml`。拒绝敏感/隐藏路径及仓库外目标；不把研究稿当源码。 |
| 行号 / 标签 | 所有 data-line-start/end 是数字，满足 `1 ≤ start ≤ end ≤ 文件总行数`；可见路径和行号与属性一致，data-symbol 与可见说明非空。11 章均有就近引用。未据此宣称符号语义被自动证明。 |
| SVG / 表格 | 5 个 SVG 逐一 XML 解析通过，均有 viewBox、role=img、非空 title/desc 且由 aria-labelledby 引用；2 个 figure 均有 figcaption，另有 2 组等价文字；13 个表格均有 caption/thead/th。 |
| 无主动资源加载 | 无 script、iframe、object/embed、base/link、表单、外部图片/媒体、事件处理器、meta refresh、src/srcset/action 等；非片段 href 仅允许源码导航。CSS 无 @import/@font-face/远程 URL/CDN，URL 引用仅允许已有本地 ID。SVG namespace URI 不视为加载请求。 |
| 残留标记 | 未发现 TODO/TBD/FIXME/PLACEHOLDER、待补充/待填/待完善/占位符、CONTENT_* 或 INSERT/REPLACE 注释标记；正文正常解释前端 placeholder 不算残留。此项为静态启发式扫描。 |
| 响应式 / 打印 | 存在 1000px/760px/400px 分支、`@media print` / `@page`、长路径换行、pre 换行、表头重复、避免图表拆分及打印解除表格滚动裁切规则。**仅证明 CSS 存在，不证明 375px/打印实际可读。** |
| 最小差异 / 保护 | 将新短语反向替换成旧短语后，全文件 SHA-256 精确回到独立审阅版本；既有其他 HTML 哈希与审阅前相同。 |

标准库解析不是完整 HTML5/CSS validator，源码行号有效也不证明所有解释正确；视觉与最终仓库门禁仍单独验收。

## 文件保护与校验值

- 审阅前目标 SHA-256：`0c0b6bc5b396c288d1753366b546c56a8d18b9e1493b906550f7edc6864058a1`。
- 一处修正后目标 SHA-256：`b2fcf85bf2d60fadf9fa9338cad368cde0d300ef0fa6344be8adf69c7514730c`。
- 非本交付 `core-architecture.zh-CN.html` 前后均为：`f19e7c16220e2db9aec2d8ac017b347cdac7d5762a76ab13f899e7ff64e9245e`；原样保留。
- 本节点唯一文件写入由 fileEditor 完成：目标 HTML 一处 str_replace，本任务 verification.md 骨架及本记录；无 shell 文件修改。

## 父代理待完成的验收

1. **A4 浏览器**：按 `file://` 直接离线打开，断网刷新；核对桌面及约 375px、目录/返回顶部/源码链接、focus、长路径与表格、图中文字、打印预览跨页及裁切。不得以本静态检查代替，也不启动产品服务。
2. **A5 仓库门禁**：实施后实际运行 `make verify`，等待终态，记录退出码和各分节结果；此前 baseline PASS 不代替本次。若失败超出文档范围，仅报告，不扩大为业务/依赖修复或云操作。
3. 父代理统一合并 A1–A6 证据并确认最终文件保护；本节点没有执行任务状态变更、归档或提交。本记录不构成完整任务通过声明。


## 父代理验收结果（2026-10-11）

### 已通过

- 前台串行 `workflow` 完成 `write-foundation → write-flows → source-review → correct-document`，所有 HTML 写节点声明同一精确 writeScope 且有顺序依赖；非作者源码审阅的唯一措辞问题已修复。
- 父代理独立执行 Python 标准库只读 HTML 检查，退出 0：11 个正文 section、44 个唯一 ID、254 处 source-ref 对应 70 个源码文件、5 个 SVG、13 个表格。目录、ARIA/SVG 引用目标、全部源码文件与行号范围有效；没有脚本、主动外部加载或所检查的占位标记。此检查不冒充完整 HTML5 验证或逐句语义证明。
- 文档 SHA-256 仍为 `b2fcf85bf2d60fadf9fa9338cad368cde0d300ef0fa6344be8adf69c7514730c`；原有其他 HTML 哈希仍与上表一致。
- **生成文档后重新运行 `make verify`，退出码 0，最终 `verify: PASS`。** Ruff、pytest、本地启动/停止脚本语法、资产 shell 语法、ESLint、TypeScript/Vite 构建及 i18n parity 各节均为 OK。pytest 有 Starlette/httpx 与 Pydantic 弃用警告，没有为消除警告修改依赖或业务代码。该结果不是之前的 baseline。
- `git diff --check` 通过；tracked 业务 diff 为空。任务目录和 docs 为本次新增，根目录另一份 HTML 原样保留；未访问真实 AWS/K8s、未运行产品服务或训练、未安装依赖。

### A4 浏览器验收受阻，未通过完整视觉验收

- `agent-browser --session tuningpad-doc-review open file:///…/docs/core-logic.zh-CN.html` 在 30 秒内没有返回，前台调用超时；后续截图命令没有执行，因此不存在可据以声称成功的截图。
- 只读 `agent-browser doctor --offline --quick` 返回 0，报告 CLI/Chromium 和该会话守护进程存在。但诊断不代表浏览器页面可用：独立后台 `get url` 等待后仍无输出，已停止该查询。
- 本会话进程检查观察到 Chromium 子进程反复变为 defunct。停止本次创建的 agent-browser 守护进程，避免继续自动拉起；未操作其他人的浏览器会话。仅记录这一观察，不推定底层沙箱或系统故障原因。
- 改用已安装的 `playwright-cli -s=tuningpad-doc-check open file:///…/docs/core-logic.zh-CN.html`（工作目录 /tmp）时退出 1：`Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome`。没有按提示安装 Chrome，也没有放宽安全设置来绕过环境问题。
- 因此**未完成实际离线刷新、桌面/375px、目录点击/源码导航、打印预览与视觉裁切验收**。静态内容、相对链接和窄屏/打印 CSS 已检查，但不能替代浏览器实测。

### 交付与剩余状态

A1/A2 的内容及独立源码抽查、A3 静态检查、A5 项目质量门禁、A6 范围保护已有证据；A4 明确保留为环境阻塞。HTML 可交付给用户直接打开，但不宣称全部验收项通过。任务保留 in_progress，不自动提交、归档或记录会触发提交的全局 journal；已生成文档不因视觉工具问题而丢弃或回滚。

## 架构介绍补充验收（2026-10-11）

用户追加“补充架构介绍文档”，在已批准 R1/R2 范围内增强原 HTML 第一章；不另建文档、不修改产品行为，也不恢复之前取消的浏览器排障。

- 前台串行 workflow：`extend-architecture` 仅有目标 HTML 写入权限，随后 `review-architecture` 独立只读核对并接收上游报告。
- 第一章与目录改为“系统总览与架构介绍”，加入 1.1 逻辑分层、1.2 物理部署、1.3 控制/采样/产物流、1.4 SQLite/FSx/S3 职责、1.5 从实现可见的取舍与扩展限制，并链接至后文深入章节；保留已有图表，不把静态归纳写成历史设计动机。
- 独立审阅未发现阻塞或非阻塞事实偏差；核对 AgentCore 与 EKS/gateway/vLLM 边界、引擎线程池/台账与跨实例局限、FSx checkpoint 和 S3 索引/导出职责、规划和成本保障的条件。只读审阅不是云端或视觉验收。
- 父代理再次运行标准库 HTML 检查，退出 0：11 个正文 section、44 个唯一 ID、284 处源码引用对应 71 个真实文件、14 张表、5 个 SVG。内部目标及源码路径/行号有效；无 U+FFFD、所检查的残留标记、脚本或自动外部加载。`git diff --check` 通过。
- 更新后 HTML SHA-256：`aae50cd648c237f32c0bbcc0c159d4754c4f11d8fa14e30a9c71905705218b39`，取代上文旧版本哈希；根目录另一份 HTML 的哈希未变。
- **此次补充后重新运行 `make verify`，退出码 0，所有分节 OK，最终 PASS。** 保留既有弃用警告，不更改业务或依赖。
- 本次只写 HTML 和本验收记录；未调用真实 AWS/K8s、未启动产品服务或浏览器。A4 实际桌面/窄屏/打印验收仍未完成，不能因本次静态与门禁通过而标记通过。

## A4 浏览器验收补测（2026-10-13，部分完成后按用户要求收尾）

- agent-browser 再次在 `open file://…` 阶段挂起（90 秒无返回，已停止并 `close --all`），与 10-11 现象一致。
- 改用 `playwright-cli 0.1.22` + 已安装的 Playwright bundled Chromium（`~/.cache/ms-playwright/chromium-1247`）。临时配置仅放在 `/tmp/docreview/cli.config.json`（`browserName=chromium, channel=chromium, headless`，并开启 `allowUnrestrictedFileAccess`，因 CLI 默认拦截 `file:`）；未安装 Chrome、未改仓库。
- 以 `file://` 成功打开目标 HTML，title 为“TuningPad 核心逻辑 · 中文源码导读”。1440×900 视口实测：控制台 0 条消息/0 错误；网络仅 1 个静态请求（文档本身），无外部请求；11 个 section、5 个 SVG 均有非零渲染尺寸（约 425–480 × 567–636 px）、14 张表；nav 12 项顺序与设计一致；`scrollWidth == innerWidth`（无水平溢出）；无破损图片；字体回退栈为系统中文字体。桌面首屏截图 `/tmp/docreview/desktop-top.png`（临时，不纳入交付）。
- **未完成**：断网刷新、约 375px 窄屏、目录/源码链接逐项点击与 focus、打印预览跨页与裁切。用户要求直接收尾，故 A4 记为“桌面渲染与无外部加载已实测，窄屏/打印/交互未验收”，不宣称 A4 完整通过。
- 文档 SHA-256 仍为 `aae50cd648c237f32c0bbcc0c159d4754c4f11d8fa14e30a9c71905705218b39`，本次未修改 HTML。根目录 `core-architecture.zh-CN.html` 已不存在（非本交付，未追查）。

### 收尾结论

A1/A2 独立源码审阅、A3 静态结构检查、A5 `make verify` PASS、A6 范围保护完成；A4 部分完成。本任务仅交付 `docs/core-logic.zh-CN.html`，无业务/依赖/README 改动。spec 无需更新（纯文档交付，未产生新的编码约定）。
