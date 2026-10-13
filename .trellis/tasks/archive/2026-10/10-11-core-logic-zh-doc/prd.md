# 核心逻辑梳理与中文 HTML 文档

## Goal / 用户价值

为首次阅读或维护 TuningPad 的中文读者提供可离线打开的源码导读：知道一次控制台操作如何进入后端任务、训练如何被规划和执行、结果如何回显，以及失败、费用和安全保护的真实边界。最终交付为 `docs/core-logic.zh-CN.html`，不是部署指南或云端验收报告。

## Background / 已确认事实

- 当前源码基线为 `f7c2b94aa91d8281ae6c03088fe6e212daf3a160`。四路只读研究已汇总于 `research.md`，本轮补规划再次核对下列关键事实；未访问云资源。父代理此前 `make verify` PASS 只计既有 baseline，不证明尚未生成的 HTML 已完成或通过验收。
- 后端是 FastAPI + SQLite + 线程池阶段引擎，前端为 React 控制台；训练算法及 rollout 协议的内部实现属于 sibling `agentcore-rl-toolkit` / verl。依赖声明见 `backend/pyproject.toml:32–35`，本任务只检查本仓库的接入契约；镜像冻结依赖的构建入口见 `trainer_image/Dockerfile:28–45`，未读取 sibling 源码。
- Job 与 Run 是不同实体及状态轴：`backend/app/models.py:118–149,203–218`；Job 的 `cancelled` 经正常 finish hook 映射为 Run 的 `stopped`，不是同名状态，见 `backend/app/pipelines/run.py:521–542`。排队取消不走该 hook；Job retry 复用原 ID/context 并跳过成功 stage，Run resume 新建 Job，monitor 的自动重试仍在同一 Job 内，见 `backend/app/jobs/engine.py:219–254,296–375`、`backend/app/routers/runs.py:293–301`、`backend/app/pipelines/run.py:392–457`。
- 文档须依据实现而非概括性口号：RayJob deadline 由可空 `max_hours` 转秒，且渲染值为真才写入（`backend/app/routers/runs.py:38`、`backend/app/services/runs.py:91–104`、`backend/app/render/train.py:525–526`）；reconciler 仅有框架，当前检索未见注册调用（`backend/app/jobs/engine.py:261–282`）；不承诺 exactly-once、全局资源同步或账单硬上限。
- `run.train` 的真实六阶段是 preflight → image → capacity → submit → monitor → finalize（`backend/app/pipelines/run.py:545–555`）。完整 checkpoint 在 FSx；训练期间 S3 保存日志和 checkpoint 索引，显式 export 才合并模型并同步 S3（`backend/app/services/runs.py:26–58`、`backend/app/render/train.py:344–357`、`backend/app/services/serving.py:35–42`）。

## Requirements / 范围与约束

| ID | 需求 |
| --- | --- |
| R1 | 中文为主，保留源码符号与必要英文术语并解释；交代产品目标、控制面/训练面/外部依赖边界，提供阅读地图与术语表。 |
| R2 | 解释启动/关闭、router/service/pipeline/engine 分工、数据模型及台账/云状态边界；分别呈现 Job、stage、Run 状态、持久化、取消、启动恢复、Job retry、Run resume 与自动 attempt 重试。 |
| R3 | 以一次 Run 为主线贯通 preview、费用确认、planner、参数/default/override 合并、实际六个 stage、Ray/verl/AgentCore/gateway/vLLM、指标/checkpoint、export/serving/eval；补充区域 setup、集群创建/导入/组件、Agent 和 Dataset 分支，不能画成每个请求必走的单流水线。 |
| R4 | 解释前端入口/页面路由、API/鉴权/错误、local draft/server snapshot/URL 状态、useLoad/usePoll/useJob、JobPanel 和 Run 向导/详情的数据流；不将轮询画成推送或共享全局缓存。 |
| R5 | 覆盖控制台以外 `start.py`、`stop.sh`、`Makefile`、`scripts/`、`cluster_assets/`、`trainer_image/`、`templates/`、公开 example 配置及 sibling toolkit 依赖；明确停止本地服务不等于清理云资源，区分完整 checkpoint 留在 FSx 与 S3 索引/日志。 |
| R6 | 说明失败恢复、清理/删除顺序、成本确认/guardian/可选 deadline、认证/网络/IRSA/registered sessions、测试隔离及其局限。区分源码事实、测试断言、静态推断、历史声明和未验证云行为；重要限制不藏在附录。 |
| R7 | 单文件自包含 HTML，内嵌 CSS 和 SVG/HTML 架构与训练时序图；不使用 CDN、远程字体、图表运行库或外部资源加载。无需运行前后端即可离线阅读；目录可跳转，源码使用有效相对文件链接及可见路径、符号/行号，不能伪称本地文件支持 GitHub 式行号锚点。 |
| R8 | 桌面/窄屏及打印可读，语义标题、目录、图表文字说明和非纯颜色标识齐备；单独拷出 HTML 仍可阅读全文，源码链接则明确依赖保留仓库相对布局。合理完整篇幅按实际内容收敛，不预定未计算字数、覆盖率或打印页数。 |
| R9 | 仅文档交付，不改业务源码、README、依赖或测试，不安装依赖。当前规划阶段只写本任务目录内文档与上下文清单，不生成 HTML、不改 task.json 或全局任务状态、不执行 task start/finish。实施须等待父代理呈现最终摘要后的用户明确批准；获批后按正常 Trellis 流程管理本任务元数据/会话状态，不将本轮禁启动误写为整个任务永久禁用生命周期管理。 |

## Acceptance Criteria / 可观察验收

| ID | 验收结果 | 对应需求 |
| --- | --- | --- |
| A1 | 目标 HTML 存在且中文正文完整；包含架构图、训练时序图、独立状态说明、数据模型、各工作流、前端数据流、安全/清理/测试与源码阅读地图；独立审阅能从正文回答 R1–R6 的关键问题。 | R1–R6 |
| A2 | 每个核心结论和图表均有真实源码路径、符号及行号/范围可追溯；独立源码审阅核对关键分支，不把研究报告或未实施计划当实现证据。正文注明快照标识和非部署验收范围。 | R1–R6 |
| A3 | 本地结构检查通过：HTML 元数据/语义结构齐备、ID 唯一、目录及文内锚点全部存在、所有源码相对链接可在仓库内解析到文件、无自动远程资源或 API 请求、SVG 引用及可访问说明有效。 | R7 |
| A4 | 以 `file://` 离线打开，断网仍可阅读正文/图表及操作目录；检查桌面、窄屏、打印预览，长路径、表格、图表无不可读裁切；源码链接在原仓库布局内可访问。 | R7–R8 |
| A5 | 实施后必须实际运行 `make verify` 并记录命令、退出码和各节结果。门禁须通过才能宣称完整验收；若环境或既有失败阻断，报告证据并标为未通过，不为此扩大到业务修复、降级检查或真实 AWS 验证。 | R6、R9 |
| A6 | 本任务内容 diff 仅含目标 HTML 与本任务规划/研究/验证材料；获批后的本任务正常生命周期记录单列，不混入其他任务状态或扩大产品范围。不含真实账号、ARN、日志、密钥、实际配置或运行数据，不改业务文件和 README；已有 `core-architecture.zh-CN.html` 原样保留且不计为交付；实施授权有清晰记录。 | R5、R9 |

## Out of Scope

- 业务缺陷修复、架构重构、新功能、翻译控制台、新增文档站点/构建器/前端依赖。
- 真实 AWS/K8s、GPU 训练、扩缩容、CloudFormation、CodeBuild、模型质量、配额/价格与部署可用性验证。
- sibling toolkit 内部源码审计、verl 算法证明、远程 CloudFormation 模板/镜像供应链审计；仅说明接入点和证据边界。
- 读取或展示 `data/`、`.run/`、真实 `config/tuningpad.yaml`、凭据与运行日志；读取 sibling 仓库、启动应用服务、修改或替代任何其他现有 HTML。允许引用的 example 配置仅用于静态说明，不拿来部署。

## Decisions / 风险与审批状态

- 技术设计和执行顺序分别落于 `design.md`、`implement.md`，证据落于 `research.md`；上下文清单位于 `implement.jsonl` 与 `check.jsonl`。
- 无阻塞产品问题。用户在父代理呈现最终方案（目标路径、内容范围、离线格式、非业务修改边界及验收方式）后回复“ok”，已明确批准实施。
- 行号随源码漂移、离线浏览器对源码文件的展示方式、测试环境依赖及既有失败均需实施时复核；云安全、竞态、清理成功及训练收益明确未验证。
