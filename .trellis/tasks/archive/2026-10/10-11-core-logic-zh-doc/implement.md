# 实现与验证流程（已获用户批准）

## 0. 审批门与现状

用户已在父代理完整呈现最终方案后回复“ok”，批准生成并验收 `docs/core-logic.zh-CN.html`。允许按正常 Trellis 流程启动与记录本任务；保持不改业务代码、依赖、README、其他 HTML、不调用真实 AWS/K8s 的边界。下文规划阶段验证记录作为历史证据保留，不再表示等待实施授权；若需求发生实质变化，重新收敛并请批。

## 1. 批准后的顺序清单

勾选项已完成；未勾选项见 verification.md 的剩余说明。

- [x] **审批/边界复核**：父代理记录用户批准的摘要版本；重新读 AGENTS、PRD/design/research/context manifests，核对工作树及 HEAD，保护未知改动；若目标 HTML 已被其他人创建，先完整读取再合并，不覆盖。
- [x] **补齐证据定位**：按 research 阅读地图核对引用路径、符号、行号和快照漂移；对四路输入未由汇总节点逐行复核的细节重新开源码。不得通过真实云查询“补证据”。
- [x] **静态骨架**：批准后仅创建 `docs/core-logic.zh-CN.html` 的元数据、语义容器、目录/章节 ID、内嵌 CSS；不改业务路由或 README。文件修改用 fileEditor，长新文件先骨架再逐段有界填充。
- [x] **正文**：按 design 章节顺序填事实/流程/出处/限制；重要风险和未验证项就近标注；补充脚本/资产/toolkit 边界及术语阅读地图。
- [x] **图表**：内嵌架构 SVG、训练时序 SVG；数据模型和多状态轨道用 HTML/CSS/SVG；每图补文字流程、figcaption、title/desc 和源码引用，核对无虚构 webhook/reconciler。
- [x] **视觉与引用**：完成窄屏/打印样式、长路径换行、表头/图注；将引用改成相对文件 href，行号/符号保留可见文本；无 JS、远程资源或运行数据。
- [x] **结构检查**：执行第 2 节规则，修复本 HTML 内的锚点、链接、SVG 引用、元数据和资源依赖问题。
- [x] **独立复核**：父代理安排非作者只读源码审阅，至少检查第 3 节全部高风险结论；作者只修正文档差异，不改业务。
- [ ] **浏览器验收**（部分完成，用户要求收尾；见 verification.md 2026-10-13 记录）：file:// 断网、目录与源码链接、桌面/窄屏/打印；浏览器工具不可用时明确 A4 未验收，不以静态扫描代替。
- [x] **仓库门禁**：实际运行 `make verify`，记录退出码/每节结果并等待终态；失败按第 4 节处理。
- [x] **最后全量复读**：对照 R1–R9/A1–A6 核对所有章节和变更边界；将独立审阅、结构/视觉/门禁结果写入任务内验证记录，明确剩余未验证项再向父代理报告。

## 2. 本地结构、锚点与源码路径验证

从仓库根目录使用 Python 标准库 `html.parser.HTMLParser`、`pathlib`、`urllib.parse` 做只读检查；可用一次性 heredoc 执行，不在业务 `scripts/` 新增工具/依赖。若要持久化校验器，只能在本任务目录以 fileEditor 创建，先审阅再运行。标准库 parser 不等于完整 HTML5 validator，必须配合浏览器和独立人工审阅。

检查规则（A3）：

1. UTF-8 解码、doctype、html lang=zh-CN、charset、viewport、非空 title；有 nav/main、单 h1、非空核心章节和两个必备 figure。
2. 收集全部 id（包括 SVG），拒绝重复；所有 `href=#…`、aria-labelledby/aria-describedby、SVG `url(#…)`/内部 href 都指向真实 ID；目录条目覆盖 design 主章节。
3. 所有 source-ref 的 href 必须是相对仓库文件路径，无 scheme/netloc/query 或假 `#Lxx`；从 HTML 父目录 resolve 后限制在 repo root 内并 `is_file()`。拒绝 data/、.run/、真实 config/tuningpad.yaml/凭据路径。可见符号/行号与 data-* 一致，范围 `1 <= start <= end <= 文件行数`。
4. 拒绝 script、iframe、object/embed、base、远程 stylesheet/font/media、meta refresh、事件处理器和表单 action。检查 src/srcset/href/xlink:href、CSS @import/url；只允许本地锚点式 SVG 资源引用及正常源码导航链接，不把 SVG namespace URI 当网络请求。
5. 每个 SVG 有 viewBox/title/desc，figure 有 figcaption 与等价文字；表格有表头/说明；CSS 含窄屏与 print 分支。
6. 报告精确问题与退出码，失败不静默忽略；有工具可用时补充本地 HTML validator，但不向在线站点上传全文、不安装外部构建体系。

辅助命令（批准后，非本轮执行）：

```bash
# 确认基线与差异；检查新增未跟踪 HTML 时不能只依赖 git diff
git rev-parse HEAD
git status --short
git diff --check
git diff --stat
# 指定文件源码定位示例
nl -ba backend/app/jobs/engine.py | sed -n '219,375p'
nl -ba backend/app/pipelines/run.py | sed -n '392,555p'
```

本地结构检查不发网络请求、不导入 app、不启动 start.py；不遍历或打开敏感运行目录。

## 3. 浏览器与独立源码审阅

### 浏览器（A4）

先加载所用浏览器工具对应 skill，再以 file:// 打开目标 HTML；不启动 FastAPI/Vite、不访问真实 API。禁止自动远程请求，离线后刷新，确认正文/图表仍完整；逐项点目录、返回顶部和代表性源码文件。检查键盘 focus、浏览器查找、长代码/路径。

建议测试一个常见桌面宽度与约 375px 窄屏，再检查打印预览（含跨页长表、源码路径、两张图和末尾章节）。宽度是测试样本而非全设备兼容承诺；记录实际浏览器/viewport/结果。打印可截图/临时 PDF 辅助，不把页数当预设验收指标、不把二进制制品纳入交付。工具不可用则保留未验收项，不伪称浏览器检查通过。

### 独立源码审阅（A1–A2）

由父代理分配非作者审阅者，只读产品源码，使用 check.jsonl、PRD/design/research 但不盲信稿件；报告事实偏差、遗漏、路径/符号错误和证据级别混淆。至少完整追踪：

- [x] RunWizard 的默认值/overrides、preview/confirm → runs.create → JobEngine.start → run.train 六阶段 → 轮询回显。
- [x] Job/stage/Run/RayJob 的状态轴，finish hook 失败例外、queued cancel 风险、retry/resume/attempt 对象身份。
- [x] planner 选择阈值/并行建议与 hard validation 的区别；完整 checkpoint/索引的存储区别。
- [x] AgentCore/gateway/vLLM 的箭头与 toolkit 边界；async rollout 不混为异步策略更新；无 FastAPI webhook/已启用全局 reconciler。
- [x] deadline 条件、费用确认例外、guardian/Run monitor 判忙区别、成功/失败/取消清理与导入集群 delete 的副作用。
- [x] Auth/SG/IRSA/registered sessions 只证明代码配置；测试 fixture 不是全网络隔离；make verify 不证明云端部署。
- [x] start/stop、scripts、templates、trainer_image、cluster_assets、公开 example 与 sibling 依赖均在阅读地图出现。

审阅发现业务疑点只记录风险/后续事项；本任务只能更正文字/图表。若发现影响 scope 的新产品决定，交父代理重新规划审批，不自选新范围。

## 4. make verify 与失败处置

批准后实施结束，从仓库根目录实际运行：

```bash
make verify
```

这是 A5 必需命令，不能只做单项替代。它调用 `scripts/verify.sh`：Ruff check/format、pytest -n auto、生命周期与 cluster/trainer/template shell 语法、ESLint、TypeScript/Vite、i18n parity。新静态 HTML 不纳入这些检查，因此 A3/A4 和源码审阅另做。

- 先检查测试隔离约定及本地工具是否可用，但不能因预判缺依赖而不尝试门禁。长命令用 background start，再 wait 等到终态；不得 sleep 等待或未结束就报告通过。
- 记录起止/命令/退出码/分节结果与安全摘要；依赖缺失、toolkit 缺失、网络安装失败与测试断言失败区分。不要把环境失败伪称代码失败或通过。
- 本节点进入时只有任务目录未跟踪；最终复读又发现非本节点创建的 `core-architecture.zh-CN.html`，保留原样，不计为本交付。业务 tracked diff 为空。若门禁暴露既有失败，用源码/差异和安全复跑定位证据，报告“疑似/确认既有”而非无证据断言；不临时回退用户工作来证明基线。
- 不自动修业务、依赖/锁文件、测试、共享规范；不减少测试、改 lint ignore、改质量命令来“变绿”。确需业务修复则另开范围并由父代理报告，不阻塞本轮规划产物。
- 即使文档检查均通过，门禁未通过仍标 A5 未通过/完整验收受阻；说明可独立验收的文档部分，不宣称全任务完工。
- 不启动 start.py、不访问真实 AWS/K8s、不构建镜像/拉 GPU；make verify 的 hermetic 测试不是授权云端实验。

## 5. 风险文件、回滚与收尾

| 风险点 | 处置 |
| --- | --- |
| `docs/core-logic.zh-CN.html` 新增或他人已创建 | 先检查存在性，已有则先读；分段 fileEditor 修改并持续结构检查，避免覆盖未知内容。 |
| 源码快照/行号漂移 | 实施前比对 HEAD，按符号重定位，更新引用；重大行为差异回研究层重收敛。 |
| 图表表达过度保证 | 独立审阅同时看图/正文；回连方向、Job/Run、可选 deadline、清理条件保持一致。 |
| 大表/长 SVG 打印裁切 | 以实际预览调整面板/分页，不通过隐藏内容解决。 |
| 无浏览器或门禁环境失败 | 标明未验收与具体证据，交父代理；不安装新产品依赖或修改业务解围。 |
| 意外 diff / 构建输出 | `git status --short` 和 tracked diff 双查；不提交 dist/cache/凭据。保护未知文件，禁止全仓 reset/clean。 |

当前 task.json 已读取但不需要修改，保持 status=planning。两个 JSONL 只放真实 spec/research 路径及 reason，不把目标 HTML/业务代码/虚假占位项塞进上下文。

本轮最终规划复读须确认：目标/范围/验收可测试、没有阻塞产品问题、PRD 无执行清单、R5 脚本/资产/sibling 与 R2 多状态轴未丢失、所有引用可定位。任务仅交父代理审批，不擅自 start/finish/提交归档；后续交付也不自动承诺验收通过。

## 6. 提交用户审批的最终摘要

父代理可将以下摘要提交用户，随后停止等待明确批准；本子代理不直接询问终端用户。

- **Goal**：新增 `docs/core-logic.zh-CN.html`，提供离线中文核心逻辑导读，帮助读者理解控制面到训练执行再到结果回显的完整链路。
- **In Scope**：后端引擎/数据模型/Job-stage-Run 状态与恢复、规划器和实际 stages、前端数据流、失败与清理、安全/成本/测试；控制台外脚本/资产/模板和 sibling toolkit 接入边界；架构/训练时序图、阅读地图与源码出处。
- **Out of Scope**：业务/README/依赖/测试修改，云部署、GPU/模型收益、真实 AWS/K8s 验证，toolkit 内部和远程模板审计，读取敏感运行信息。
- **Acceptance**：单文件无需服务/外网可打开；内嵌图表、目录锚点和源码相对链接有效；中文、窄屏/打印可读；结构检查、独立源码审阅、离线浏览器检查及实际 `make verify`。门禁失败如实记录，不扩大为业务修复，也不宣称完整验收。
- **Decisions**：原生 HTML/CSS/inline SVG、无外部依赖和默认无 JS；文件路径链接+可见符号/行号；明确源码快照不是部署验收，不混淆 Job 与 Run，不把可选 deadline/guardian 说成硬预算。
- **Risks**：行号漂移、离线源码链接依赖仓库布局、浏览器/测试环境待验证；queued cancel/hook 同步等是静态风险未复现；实际云安全、清理效果及训练收益未验证。无阻塞产品问题。
- **Artifacts**：`prd.md`、`research.md`、`design.md`、`implement.md`、`implement.jsonl`、`check.jsonl`；task.json 保持 planning；目标 HTML 尚未创建，其他已有 HTML 原样保留。

**审批请求的含义**：请用户确认这份最新摘要后再进入实施；创建任务的既有许可不替代该确认。

## 7. 本规划节点的收敛与验证记录

- 已将初始 TBD PRD 改写为 Goal/Background/Requirements/Acceptance/Out of Scope/Decisions 结构，并最终通读当前完整 PRD。R1–R9/A1–A6 映射保留；执行清单在本文而非 PRD；非控制台资产/sibling 与 Job/Run 区别未遗漏；无阻塞产品问题。
- 最终复读发现 PRD 的并行补充（父代理 baseline PASS 记录、其他 HTML 保留、批准后生命周期措辞），均保留未覆盖。本节点未独立验证该 PASS；须由父代理提供原始命令证据，且仍不能替代实施后的 A5。
- 只读 Python 检查解析两个 JSONL：implement 10 项、check 8 项，均有非空 reason、真实存在的 spec/research 路径且无重复/占位。
- 对规划正文中可解析的 195 处源码路径+数字行号引用检查文件存在及行号不越界，全部通过；这不是全部语义结论逐行验收。核对无待填骨架标记、task.json.status=planning、目标 `docs/core-logic.zh-CN.html` 不存在。
- `git diff --check` 返回 0，tracked diff 为空；任务目录与其他节点新增 `core-architecture.zh-CN.html` 为未跟踪项，所以该 diff 命令不覆盖它们。本节点只以 fileEditor 修改本任务目录文件，未读写其他 HTML、未写业务代码或 README、未切换全局任务状态。
- 本节点未运行 make verify/浏览器/真实云操作；后续验证按 A3–A5 执行，当前不可宣称 HTML 或完整任务验收通过。
