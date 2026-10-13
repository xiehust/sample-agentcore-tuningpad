# 单文件离线中文 HTML 设计

状态：用户已在最终方案摘要后回复“ok”批准实施。以下设计是本次 HTML 交付的约束；历史“当前规划阶段”说明描述审批前状态。

## 1. 变更边界与设计原则

- **最小差距**：源码和已有指南分散，缺少一份中文、离线、自包含且跨层可追溯的核心逻辑导读；不缺一个新控制台或训练实现。
- **行为所属位置**：阅读体验属于新静态文档 `docs/core-logic.zh-CN.html`；当前业务行为继续由现有后端、前端和资产决定，文档不介入。
- **当前可写范围**：仅 `.trellis/tasks/10-11-core-logic-zh-doc/` 的 PRD、设计、执行方案、研究与上下文清单；保留 task.json 的 planning 状态及身份信息，不写任何全局指针。
- **批准后交付边界**：只新增目标 HTML，必要验证记录仍在任务目录；不改 README、业务源码、依赖/锁文件、测试、共享 spec、启动脚本或云配置。无需业务重构/数据库迁移。
- **证据优先**：`research.md` 为跨层底稿，源码是最终依据。开篇给出 commit 快照及静态阅读范围；每节就近引用路径/符号/行号，显式区分实现、测试断言、静态推断、历史声明、外部未验证。
- **简单可交付**：原生 HTML5、内嵌 CSS/inline SVG；不使用 Mermaid/图表库、CDN、远程字体或 bundler。默认无 JavaScript，不做搜索、折叠核心正文或动态源码读取。全文静态存在，浏览器原生查找足够。

## 2. 信息架构与内容契约

采用“总览 → 台账与状态 → 一次训练 → 用户反馈 → 失败/安全 → 阅读地图”的叙事，而非逐文件清单。中文正文中保留精确英文标识。页面开头为标题、适用读者、快照/范围说明、重要限制摘要与目录；后面为 main/article，末尾术语/源码地图及验证范围。

| 建议 section ID | 标题与必须回答的问题 | PRD / 研究依据 |
| --- | --- | --- |
| overview | 系统在做什么？控制面、训练面、外部依赖；总架构图 | R1；research §1 |
| lifecycle | 如何启动/关闭？模块职责、本地 scripts/Makefile/config 与 sibling/toolkit 边界 | R2、R5；§1 |
| data-model | Project/Cluster/Agent/Runtime/Dataset/TrainerImage/Run/Metric/Export/Endpoint/Eval/Job 怎么关联？台账不等于云真相 | R2；§2 |
| job-engine | Job/stage 与业务 Run 分离的状态图/表；线程池、持久化、取消及恢复语义 | R2；§2–3 |
| preparation | setup/cluster create-import-components/Agent/Dataset/Trainer 各分支及真实 stage | R3、R5；§4 |
| planning | FSDP/Megatron/full/LoRA/TP/CP/EP、defaults/overrides 与 planner 的启发式局限 | R3；§5.1–5.2 |
| training-flow | 一次 Run 跨层时序、六阶段、Ray/verl/AgentCore 模型请求回连、指标/checkpoint | R3；§3.3、§5 |
| frontend-flow | 页面入口/URL、API/AuthGate/errors、三类状态、hooks/JobPanel 与 RunWizard/RunDetail | R4；§6 |
| recovery | 启动恢复/Job retry/Run resume/attempt；失败守卫、export/serving/eval、Run 清理及集群删除 | R3、R6；§3.2、§7 |
| safety-tests | 成本四层条件、鉴权/网络/IRSA/会话、测试隔离/质量门禁与未验证项 | R6；§8–9 |
| source-map | 按问题阅读地图、术语、源码引用规范、离线阅读/源码相对路径说明 | R1、R5、R7；§10 |

章节可按内容密度拆小标题，不以未经计算的页数/字数作为质量目标。每个关键章节具备结论、流程/表格、关键源码、限制四部分；不重复整段研究稿。正文必须能独立理解，不依赖访问源码链接才能看懂。

重要纠偏就近出现：Job cancelled / Run stopped；没有注册 reconciler；deadline 可空；smoke 可跳；checkpoint 完整数据在 FSx；resume 无再次费用确认；清理尽力而为；导入集群 delete 的前置清理仍有云副作用；toolkit 内部未读。研究稿中的疑点保持“静态风险、未复现”标签，不转成缺陷修复清单。

## 3. 图表与源码引用契约

### 必备两图

**架构图**采用 inline SVG，分区绘制：

1. 浏览器：AuthGate/页面 → lib/api，HTTP 请求与资源/Job 轮询。
2. 本地控制面：FastAPI/router → services/pipelines → JobEngine；SQLite 台账、Job 文本日志旁置。标明线程池非分布式队列。
3. AWS/EKS：CloudFormation/HyperPod 或 EC2 池、ECR/CodeBuild、S3/FSx；RayJob 驱动训练；AgentCore 与 gateway/vLLM 的模型调用。
4. guardian 作为集群内独立 CronJob 用独立虚线连接缩容目标；外部 toolkit/verl 框标“仅接入契约，内部未审计”。

箭头区分 HTTP、编排、读写产物与模型回连；用文字/线型而非只用颜色。不得画出常驻全资源 reconciler 或 FastAPI rollout webhook。

**训练时序图**采用 inline SVG，按编排/训练/回显分组或拆为同一 figure 内相邻面板，避免横向无限加泳道：

- draft → preview/plan → 费用确认 → POST create；Run commit 与 Job commit 分开。
- Job 六阶段；image 分支指独立 trainer.build；capacity 区分 provider；submit 体现确定性名称与已有检查。
- Ray/verl/toolkit 调 AgentCore；Agent 模型请求经 gateway 到 vLLM；奖励/结果按外部契约返回训练侧。
- S3 日志/索引、FSx checkpoint → monitor/Metric/Run；UI 单独轮询，不画成后端主动推送。
- 旁路标记自动 attempt 重试、预算/取消/失败守卫、finalize 与条件缩容；复杂例外用邻接表，不把所有细节压进主图。

每图使用 viewBox、明确 title/desc 和 figcaption，旁边有等价编号文字流程及出处。SVG 内不放外链 image/use、foreignObject 或脚本。

### 其他图表

数据模型用紧凑 HTML/CSS 关联表或 SVG，明确 FK 与 Job.target_id 的逻辑关联区别。状态展示分 Job/stage/Run 三条轨道，外部 RayJob 另列映射；不要仅用一个“pending→running→completed”图抹掉真实状态。pipeline 矩阵和失败恢复表用 HTML，便于打印和复制。

### 源码引用

- 每节给出相对文件链接，例如从目标位置指向 `../backend/app/jobs/engine.py`，可见标签为 `backend/app/jobs/engine.py:219–254 · JobEngine.retry/resume_pending`。
- 不使用 `#L219` 假装 file:// 源文件能跳行；用户按可见行号/符号定位。单独拷出 HTML 时正文仍完整，源码链接需要原仓库布局。
- 为结构检查统一使用 `class=source-ref`；可在引用上附 `data-line-start/data-line-end`（整数）和 `data-symbol`，href 是路径本身。范围有效只能证明可定位，语义仍由独立源码审阅确认。
- 只链接本仓库存在的非敏感文件；sibling/云 URL 可作为边界文字，不把外部行为包装成已审计实现，也不使验收依赖外网。
- 核心结论须含符号和行号/范围；目录型资产可先列路径，再为所讲行为补定位行或配置 key。不要把待实施 Trellis 文档当作产品实现出处。

## 4. 离线、响应式、无障碍与打印

- `<!doctype html>`、`lang=zh-CN`、UTF-8、viewport、明确 title；只一个主 h1，标题层级有序，nav/main/article/section/figure/table 使用原生语义。
- 全部样式在 head 的 style，图表 inline SVG/HTML/CSS；字体用系统中英文字体栈，不嵌 CDN、外部 CSS、远程字体、iframe、对象、遥测、表单 action 或 API 请求。
- 阅读型浅色版式、足够行距、正文限宽。桌面目录可 sticky，窄屏转正常文流；内容列 min-width:0，长路径/标识 overflow-wrap，代码保留换行。
- 表格明确 caption/表头；窄屏允许单个表格区域受控横滚，但正文不横向溢出。图表优先拆面板而非把字缩到不可读，等价文字始终可见。
- 目录/返回顶部使用本地锚点；focus-visible 可见，提供跳到正文入口；不要求 JS 才能跳转。事实/风险/外部边界用文字标签+边框/线型，灰度也能区分。
- SVG title/desc 通过 aria-labelledby 引用真实唯一 ID；每张图有文字流程。无动画、无交互隐藏关键说明。
- `@media print` 去掉 sticky 与无关导航，白底深色，保留出处；解除滚动裁切，表头可重复，短 figure/表格尽量避免跨页，长表自然分页，不对整章强制 break-inside:avoid。打印宽图必要时改布局/分面板，不能只 overflow:hidden。
- 离线验收既看无网络依赖，也看直接 file:// 的锚点与文件链接；本地源码可能被浏览器展示或下载，属于浏览器行为，文档不承诺跳行阅读器。

## 5. 兼容、交付与风险取舍

文档不挂进 React 路由、不改 README、不引入双语 locale key；前端“t()+双语键”约定适用于控制台业务文本，这里按用户要求交付独立中文文档，不制造运行时 i18n 依赖。正文以现代浏览器的原生 HTML/CSS/SVG 能力为基线，不承诺尚未测试的全浏览器矩阵。

手写 SVG 增加少量维护成本，换取真正离线、无需图表运行库；图旁文字和源码证据便于独立检查。单文件复制便利，但源码链接仍依赖仓库相对布局；这个限制放在首页/源码地图明示。引用 commit 与路径/符号并存，便于行号漂移时复查。

验证分三个独立面：HTML 结构/锚点/路径检查，浏览器离线/窄屏/打印检查，独立源码审阅；`make verify` 是额外必跑仓库门禁，不覆盖新 HTML 的内容真实性/可读性。门禁既有失败或缺依赖只记录，不在文档任务里修改业务补洞。

无生产 rollout 或数据 migration。批准后如果文档不合格，仅回退本任务所新增 HTML/对应规划修订，保护其他工作；不要用全仓 reset/clean 或删除未知文件。当前不执行任何回滚、部署、任务 start/finish 或全局状态写入。
