# sample-agentcore-launchpad 架构调研（供 tuningpad 复用）

源仓库：`/home/ubuntu/workspace/sample-agentcore-launchpad`（HEAD `ef96c77`）。一句话：**React 18 + Vite SPA（自研组件库，无 UI 框架）+ FastAPI 单体控制面 + SQLite 台账 + boto3 直连 AgentCore + 一个 CDK 共享栈**。所有资源都打到真实 AWS 账号（默认 `us-west-2`），没有 mock 层。

权威文档：`docs/architecture.md`（3500+ 行，按功能写明映射到哪个 AWS 资源）、`CLAUDE.md`/`AGENTS.md`（内容互为镜像，`backend/tests/test_agents_md_mirror.py` 校验两者不漂移）。README 和 CLAUDE.md 里引用的 `docs/agent-runbook-dev.md`、`docs/agent-runbook-prod.md`、`docs/setup.md` 在仓库中**不存在**（只有 `docs/setup.zh-CN.md`）。

---

## 1. 技术栈与精确版本

版本号取自 `frontend/package-lock.json`、`backend/uv.lock`、`infra/uv.lock`。

### 前端 `frontend/`
| 项 | 选型 | 锁定版本 |
|---|---|---|
| 框架 | React + react-dom | 18.3.1 |
| 构建 | Vite（`@vitejs/plugin-react` ^4.3.4） | 6.4.3 |
| 语言 | TypeScript strict | 5.9.3 |
| 路由 | react-router-dom（`BrowserRouter` + `<Routes>`，页面用 `React.lazy` 懒加载） | 6.30.4 |
| i18n | i18next + react-i18next + i18next-browser-languagedetector | 24.2.3 / 15.7.4 / ^8.0.0 |
| UI 库 | **不用第三方 UI 库**，组件全部自研：V2 在 `src/v2/ui.tsx`（1123 行），classic 在 `src/components/`。`@cloudscape-design/*`（components 3.0.1329）虽在依赖里，但 `src/` 中**没有任何 import**，只是为 `bedrock-agentcore` 的浏览器 live-view 做 dedupe | — |
| 样式 | 纯 CSS + CSS 变量（无 Tailwind、无 CSS-in-JS）：`src/theme/tokens.css`、`src/theme/app.css`、`src/v2/v2.css`、各页面自己的 `*.css` | — |
| 图标 | lucide-react | 1.24.0 |
| 状态管理 | **不用 Redux/Zustand/React Query**。用 React Context（`auth/auth-context.ts`、`workspace/workspace-context.ts`、Toast），自写 `useLoad(fetcher, key)` / `usePaged` 两个 hook（`src/v2/hooks.ts`），偏好设置走 `useSyncExternalStore` + localStorage（`lib/ui-version.ts`） | — |
| 其他 | `@xyflow/react` 12.11.2（Studio 画布）、`@monaco-editor/react` 4.7.0、`react-markdown` 10.1.0 + remark-gfm + rehype-highlight、`bedrock-agentcore` 0.4.0（JS SDK，用于 browser live view） | |
| 字体 | classic：`@fontsource-variable/archivo` 5.2.8 + `@fontsource/ibm-plex-mono` 5.2.7；V2：系统字体栈 | |
| Lint | eslint 9 + typescript-eslint 8 + react-hooks 插件 | |

### 后端 `backend/`（Python ≥ 3.12，用 **uv** 管理）
| 项 | 锁定版本 |
|---|---|
| fastapi（starlette 1.3.1） | 0.139.0 |
| uvicorn[standard] | 0.51.0 |
| sqlalchemy 2.x（`Mapped[]` 声明式） | 2.0.51 |
| pydantic / pydantic-settings | 2.13.4 / 2.14.2 |
| boto3 | 1.43.83 |
| bedrock-agentcore[simulation]（预览版 SDK，固定 `==1.17.*`） | 1.17.0 |
| 其他 | pyyaml、playwright、claude-agent-sdk、pypdf、pillow；dev：ruff、pytest、pytest-xdist、httpx |

ruff 配置：`line-length=100`，`target-version=py312`，规则 `E,F,I,W,UP,B`。

### Infra `infra/`
CDK v2 Python：`aws-cdk-lib` 2.261.0，`constructs` 10.x。入口是 `infra/app.py`，只定义一个栈 `launchpad-base`（`stacks/base_stack.py`）；另有 `video_app.py` / `stacks/video_stack.py` 处理视频资源。

---

## 2. 目录结构

```
AGENTS.md / CLAUDE.md      agent 指南（两份互为镜像）
Makefile                   dev / verify / bootstrap / backend / frontend
start.py / stop.sh         后台起停本地栈（PID 和日志写到 .run/）
config/                    launchpad.example.yaml（入库）；launchpad.yaml（bootstrap 生成，gitignore）
data/                      SQLite 台账 launchpad.db、skill-lab 产物、venv（gitignore）
backend/app/
  main.py                  create_app()：中间件、router 注册、启动时 resume 未完成作业
  core/                    config.py（Settings）、db.py、errors.py（统一错误信封）、route_policy.py（默认拒绝的鉴权表）
  routers/                 /api/* 各模块 + public_api.py（/v1）
  services/                aws_clients.py（全仓唯一创建 boto3 client 的地方）、workspace.py、agentcore/*（预览 API 包装层）
  deployer/                pipeline.py（五阶段流水线）+ harness/zip_runtime/container/byoc
  evaluation/ optimization/ skill_lab/ assistant/ codegen/ system_agents/ templates/
  models/ schemas/
backend/tests/             hermetic 单测（SQLite 指向临时库、AWS 打桩）；scripts/e2e_*.py 打真实 AWS
frontend/src/
  main.tsx App.tsx i18n.ts
  auth/ workspace/         AuthGate、WorkspaceProvider
  layout/                  classic 外壳：Shell / Sidebar / Topbar / nav.ts
  components/              classic 组件：Panel、StageCard、LaunchSequence、DataTable、Chip、Btn…
  pages/                   classic 页面
  v2/                      V2 控制台（默认）：V2Shell.tsx、nav.ts、ui.tsx、hooks.ts、v2.css、pages/*
  lib/api.ts               唯一的类型化后端客户端（约 4800 行）
  locales/{en,zh-CN}/common.json   各约 7500 行，key 必须一一对应
infra/                     CDK app + spoke/launchpad-workspace-role.yaml（跨账号 spoke 角色的 CFN 模板）
apps/studio/               vendored Strands Studio（React 19 + Tailwind 4，有自己的约定）
vendor/skillopt/           vendored SkillOpt 训练引擎（只以子进程方式运行）
scripts/                   bootstrap.py teardown.py dev.sh verify.sh i18n_check.py i18n_zh_punct.py exec-image/Dockerfile
design/                    HTML mockup + 截图（见 §11）
docs/                      architecture/api/setup/troubleshooting + lab/workshop 教程（截图很多）
samples/                   byoc、datasets、policies、skills…
```

---

## 3. 后端：框架、API 风格、鉴权

- **FastAPI 单体**。`app.main:create_app()` 注册 25+ 个 router。OpenAPI 文档在 `/api/docs`，schema 在 `/api/openapi.json`。
- **REST + JSON**，路径前缀：
  - `/api/*`：给控制台用，cookie 会话鉴权。
  - `/v1/*`：公开集成 API，`X-Api-Key` 鉴权（key 以 sha256 存储），支持同步和 SSE。
- **长作业约定**：`POST` 返回 `202 {agent, job_id, deployment_id}`，之后用 `GET /api/jobs/{id}` 取 `{id,type,status,error,events[]}`。
- **错误信封**：`app/core/errors.register_error_handlers` 统一返回 `{code, message, detail}`。前端 `ApiError` 按 `code` 做本地化（`localizedMessage`）。
- **鉴权**（`routers/auth.py`）：
  - 签名 cookie `launchpad_session`，TTL 12h。
  - 内置 admin 完全由配置驱动（`LAUNCHPAD_AUTH_USERNAME` / `LAUNCHPAD_AUTH_PASSWORD`），没有数据库行。
  - 另有 `users` 表支持自助注册：默认 `pending`，需管理员审批，有效期 7 天。
  - 角色在每次请求时从数据库读取，不存进 cookie。
  - **不设密码 = 关闭登录**：只允许 loopback 访问；`run_mode=prod` 且无密码时拒绝启动。
- **授权**：`core/route_policy.py` 是一张对所有 `/api` 路由的**默认拒绝表**。取值为 `ADMIN` / `MEMBER` / `PUBLIC` / `perm:agents.deploy` 等，另有一维表示是否属于 workspace 作用域。`tests/test_route_policy.py` 枚举所有路由，表与路由不一致即失败。
- **Workspace（多账号/多区域）**：前端每个请求带 `X-Workspace` 头（`lib/workspace-header.ts` 在首次渲染前安装 fetch 包装）。后端 `resolve_workspace` 解析出 `WorkspaceContext(account, region, role_arn, external_id)`，跨账号通过 AssumeRole（ExternalId，1h 会话，可自动刷新）。
- **配置优先级**：默认值 < `config/launchpad.yaml` < `LAUNCHPAD_*` 环境变量 < init kwargs。关键 key：`region`、`database_url`、`cors_origins`、`run_mode`、`auth_*`、`resources.{artifacts_bucket, ecr_repo(_uri), codebuild_project, user_pool_id, execution_role_arn, registry_id, memory_id}`、`model_prices`、`skill_lab_*`。

## 4. 如何调用 AWS

- **运行时全部用 boto3**，不用 CDK：per-agent 资源（Runtime、Harness、Gateway target 等）由后端走 "boto3 fast path" 直接创建，标识符记入台账。
- **唯一入口** `backend/app/services/aws_clients.py`：按 `WorkspaceContext` 缓存 session/client，并禁止传入 `region_name` / 凭证参数。`tests/test_client_funnel.py` 会扫描代码，在别处出现 `boto3.client(...)` 即失败。
- AgentCore 的 client 名称和预览 API 漂移只在 `services/agentcore/{client,runtime,harness,registry,codebuild,gateway,policy,evaluation}.py` 里处理，其他代码一律显式接收 client，便于测试注入桩。
- **CDK 只用于共享底座**，即栈 `launchpad-base`，包含：
  - S3 `launchpad-artifacts-{acct}-{region}`
  - ECR `launchpad-agents`（MUTABLE，部署时按 digest 引用）
  - CodeBuild 项目 `launchpad-agent-builder`（构建 ARM64 镜像）
  - Cognito User Pool + M2M client
  - Agent 执行角色、Gateway/KB 角色、demo Lambda 和 API GW

  这些资源通过 CfnOutput 输出，由 `scripts/bootstrap.py` 回写到 `config/launchpad.yaml`。
- **Bootstrap**（`make bootstrap` = `cd backend && uv run python ../scripts/bootstrap.py [--skip-cdk]`），幂等，按顺序：
  1. 缺失时执行 `cdk deploy launchpad-base`；
  2. 安装固定版本的 `@aws/agentcore@0.21.1` CLI 到 `data/agentcore-cli`；
  3. ensure Registry / Memory / Gateway / Transaction Search；
  4. 写出 yaml。

## 5. 长作业与进度展示（对 RL 训练最关键）

**原则：没有 WebSocket，作业进度一律前端轮询；SSE 只用于聊天/LLM 流式输出。**

### 部署流水线 `backend/app/deployer/pipeline.py`
- 阶段固定为 `STAGE_ORDER = generate → package → provision → deploy → register`。各方法用 `register_method(name, {stage: fn})` 注册自己的阶段函数，方法模块在 `main.py` 中按副作用导入。
- 作业在 `threading.Thread(daemon=True)` 后台线程执行（`start_deploy_async`）。
- 每个阶段的 `{name,status,detail,started_at,ended_at}` 写入 `Deployment.stages`（JSON 列）；事件以 JSONL 追加到 `Job.log`。
- 启动时 `resume_pending_jobs()` 从第一个未成功的阶段继续，因此**阶段必须幂等**。

### 前端展示
- `pages/CreateAgent.tsx` 用 `setInterval(poll, 2000)` 并行轮询 `getAgent` 和 `getJob`。
- 进度由 `components/LaunchSequence.tsx` + `StageCard.tsx` 渲染为阶段条：✓ / ● / ✕ / 序号，配合状态 Chip（`active` / `failed` / `deploying`）。

### Skill Lab（最接近 "训练平台" 的现成实现，强烈建议照抄模式）
后端在 `backend/app/skill_lab/`：
- `jobs.py`：有界并发队列 `EvalRunQueue(max_concurrency=skill_lab_max_concurrent_jobs)`，每个作业起一个 vendored CLI 子进程（`vendor/skillopt/scripts/train.py`），运行在独立 venv `data/skill-lab-venv`，环境变量走白名单。取消时 kill 整个进程组；后端重启后把非终态作业标记为 `interrupted`，训练作业可按 checkpoint `resume`。
- 状态集合：`queued | running | succeeded | failed | cancelled | interrupted`。
- 路由（前缀 `/api/skill-lab`）：
  - `GET/POST /jobs`、`GET /jobs/{id}`
  - `POST /jobs/{id}/cancel|resume|publish`、`DELETE /jobs/{id}`
  - `GET /jobs/{id}/log?offset=` 按字节偏移增量读日志，返回 `{content,next_offset,eof}`
  - `GET /jobs/{id}/train-summary`：训练中途即可读，`steps[]` 逐步增长；没有数据时返回 404 `skill_lab.results_pending`
  - `GET /jobs/{id}/results|diff|artifacts?path=|artifacts/raw?path=`（带路径穿越防护 `_safe_resolve`）
- 拓扑：编排子进程在后端主机上运行；每个任务的 rollout 跑在 AgentCore Runtime `launchpad_skill_lab_worker` 的 microVM 会话里（`idleRuntimeSessionTimeout=300`，`maxLifetime=28800`，session storage 挂载在 `/mnt/workspace`）。

前端在 `frontend/src/v2/pages/skilllab/`：
- 轮询常量（`lib/skillLab.ts`）：`JOB_POLL_MS=2500`，`LIST_POLL_MS=8000`。
- `JobLog.tsx`：offset 增量日志面板，作业 live 时自动滚动到底部。
- `TrainCurve.tsx`：内联 SVG 绘制验证分数曲线，y 轴固定 0–1，区分 hard/soft/baseline/best。
- `TrainDetail.tsx`：步骤时间线，显示 ACCEPT/REJECT 门控结果。
- `TrainWizard.tsx`、`JobList.tsx`、`ArtifactBrowser.tsx`、`state.ts`（`useJobList` / `useJob` 轮询 hook）。

### SSE
- 前端：`lib/chat.ts`、`lib/assistant.ts` 用 `fetch` + `res.body.getReader()` 自己解析 `text/event-stream`（不用 EventSource，因为需要 POST body 和自定义头）。
- 后端：`StreamingResponse(media_type="text/event-stream")`，见 `routers/codegen.py`、`execution.py`、`public_api.py`、`services/chat.py`。心跳问题见 `docs/issues/2026-07-16-studio-agent-sse-heartbeat.md`。

## 6. 数据持久化

- **SQLite**：`data/launchpad.db`，SQLAlchemy 2，`database_url` 可替换。
- **原则**：AWS 是事实来源，台账只存标识符和派生进度。
- 主要表：`workspaces`、`users`、`user_workspaces`、`agents`（`spec` 为 JSON）、`deployments`（`stages` 为 JSON）、`jobs`（`type`、`status`、`payload` JSON、`log` JSONL、`error`）、`chat_*`、`api_keys`、`eval_*`、`experiments`、`skill_lab_tasksets`、`skill_lab_jobs`。
- `core/db.py:WORKSPACE_SCOPED_TABLES` 列出所有按 workspace 隔离的表。
- 大文件产物放本地 `data/skill-lab/<job>/`（日志、out/ 目录）以及 S3 `skill-lab/exec-jobs/` 前缀（7 天 TTL 清理）。

## 7. 部署与运行方式

**本地开发**（前置：uv ≥ 0.8、Node ≥ 20、CDK CLI v2、ARM64 Docker 仅 container 方式需要，以及 `cdk bootstrap aws://<acct>/us-west-2`）：
```bash
cd backend && uv sync && cd ../frontend && npm install && cd ../infra && uv sync && cd ..
make bootstrap                 # CDK 栈 + AgentCore 单例资源 + 写 config/launchpad.yaml
./start.py                     # 后台启动：backend :8000（--reload）+ vite :5173，日志/PID 在 .run/
./start.py --prod              # 先 vite build，再用 vite preview + 无 reload 的 uvicorn，绑定 0.0.0.0
./stop.sh                      # 只停 start.py 拉起的进程组
make dev                       # 前台运行（scripts/dev.sh）
make backend / make frontend   # 单独启动
make verify                    # 门禁：backend ruff+pytest -n auto、infra ruff+pytest、frontend eslint+tsc+build、i18n 一致性、中文全角标点
```
- 端口：`PLATFORM_API_PORT=8000`，`PLATFORM_UI_PORT=5173`。绑定地址用 `LAUNCHPAD_HOST` / `LAUNCHPAD_API_HOST` 覆盖。
- Vite 把 `/api`、`/v1` 代理到 `LAUNCHPAD_API`（默认 `http://localhost:8000`），见 `frontend/vite.config.ts`。

**生产**：单台 EC2，systemd 管理进程，前面挂 nginx（origin-key 校验）+ CloudFront，需设置 `LAUNCHPAD_AUTH_PASSWORD` 和 `LAUNCHPAD_AUTH_COOKIE_SECURE=true`。没有容器化或 ECS 部署。Workshop 用的 EC2 CFN 模板在 `docs/workshop3/static/infrastructure/workshop-ec2.yaml`，IAM 策略在 `docs/workshop3/static/iam/participant-policy.json`。

## 8. 设计系统 Token

存在两套主题。**V2 是默认**：`lib/ui-version.ts`，localStorage key `launchpad_ui_version`，只有显式存了 `v1` 才回到 classic。新平台**建议照搬 V2**。

### V2（浅色企业 SaaS 风格，配色接近 Arco Design）`src/v2/v2.css`，作用域限定在 `.v2` 或 `body.v2-body`
```
--v2-primary:#1664ff  hover:#4080ff  active:#0e42d2  soft:#e8f3ff  line:#bedaff
--v2-bg:#f4f6fa  --v2-card:#fff  --v2-fill:#f7f8fa  --v2-fill-2:#f2f3f5
--v2-line:#e5e6eb  --v2-line-2:#f2f3f5
--v2-ink:#1d2129  ink-2:#4e5969  ink-3:#86909c  ink-4:#c9cdd4
--v2-success:#00b42a/#e8ffea  --v2-warning:#ff7d00/#fff7e8  --v2-danger:#f53f3f/#ffece8
--v2-radius:8px  --v2-radius-sm:4px
--v2-shadow:0 1px 2px rgba(0,0,0,.04),0 2px 8px rgba(0,0,0,.04)
--v2-font:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Segoe UI",Roboto,...
--v2-mono:"SFMono-Regular",Menlo,Consolas,monospace
正文 14px / line-height 1.5715；页面标题 h1 20px/600；曲线辅色 #14c9c9
```
布局：
- **顶栏** `.v2-top`：高 56px，白底，sticky。从左到右依次是 Logo + 品牌名 + `V2` 小徽标、产品 tab（`.v2-top-tabs` 分段控件）；右侧为 Workspace 选择器、中/英切换、"切回经典版"、头像 + 用户名 + 登出。
- **侧栏** `.v2-side`：宽 216px（折叠后 60px），sticky，`top:56px`。按分组展示，组头可折叠（状态存在 `launchpad_v2_nav_collapsed`）；激活项为 `primary-soft` 背景 + 左侧 3px 蓝条。
- **内容区** `.v2-main`：`padding 20px 24px 40px`，`.v2-main-inner` 最大宽 1480px、居中。
- 页面模式：
  - 列表页：`PageHeader`（标题 + 描述 + `SubTabs` + 右侧操作）→ 过滤栏（`FilterSelect`、`SearchInput`，显示 "共 N 项"）→ `Table` + `Pager`。
  - 向导/详情页：`FlowHeader`（返回按钮、标题、居中的 `Steps` 分段步骤条、右侧上一步/下一步）→ 若干 `Card`，卡片标题带 `|` 前缀竖线。

### Classic（深色 "仪表盘终端" 风格）`src/theme/tokens.css`，从 `design/mockup.html` 原样移植
```
--bg:#0B0E0D --panel:#141816 --panel-2:#191E1B --line:#232B27 --line-2:#2E3833
--ink:#E9EDEA --ink-2:#A3ACA6 --ink-3:#69736C --amber:#FFB000（品牌色，只用于 chrome）
图表系列 --s1:#3987E5 --s2:#199E70 --s3:#C98500 --s5:#9085E9
状态色 --good:#0CA30C --warn:#FAB219 --serious:#EC835A --crit:#D03B3B
--sans:"Archivo Variable"  --mono:"IBM Plex Mono"
```
视觉特征：顶栏高 52px，背景叠加网格线和胶片颗粒；侧栏带 `01`、`02` 编号和分组（平台 / 运维 / 管理），近直角边框。

## 9. 页面清单与向导流程

### V2 导航 `src/v2/nav.ts`（分组 → 路由）
| 分组 | 页面 |
|---|---|
| 首页 | `/v2` |
| 构建 | `/v2/assistant`（AI 架构师）、`/v2/agents`（Agent 管理）、`/v2/registry`、`/v2/knowledge-bases` |
| 运行 | `/v2/chat`、`/v2/observability`、`/v2/memory`、`/v2/governance` |
| 评估 | `/v2/eval/insights`、`/eval/data`（数据中心）、`/eval/tasks`、`/eval/online`、`/eval/evaluators`、`/eval/experiments`、`/v2/skill-lab` |
| 学习 | `/v2/videos` |
| 管理（admin） | `/v2/users`、`/workspaces`、`/announcements`、`/video-management` |

Classic 路由为 `/`、`/agents[/new|/import|/:id|/:id/edit]`、`/create/studio`、`/create/assistant`、`/registry`、`/knowledge-bases`、`/memory`、`/chat`、`/observability`、`/evaluation`、`/skill-lab`、`/governance`、`/users`、`/workspaces`。

**子页面约定**：用 `?view=` / `?tab=` / `?id=` 查询参数表示，不用嵌套路由。例如 `/v2/skill-lab?tab=train&view=new|detail&id=`、`/v2/agents?view=detail&id=`。新页面应遵循这一约定。

### 主要向导
1. **创建 Agent**（`v2/pages/agents/AgentWizard.tsx`）：三步 `选择方式 → 配置 → 确认`。
   - 方式：`harness | zip_runtime | container | byoc`，配置卡片在 `MethodSections.tsx` / `wizardKit.tsx`，确认页在 `WizardReview.tsx`。
   - 提交 `POST /api/agents` 得到 202，随后跳到详情页，轮询五阶段流水线和 job log。
   - URL 预填：`?method=`、`gateway=`、`skill=`。
2. **Skill Lab 训练**：`TrainWizard`（选 Skill、任务集 split、模型/后端、参数，其中 `PARAM_BOUNDS workers 1–8 / timeout 60–3600s / limit 0–10000`）→ `TrainDetail`（状态、日志、分数曲线、步骤时间线、SEED→BEST diff、resume、publish）。
3. **评估运行 / 实验 A/B / 金丝雀**：`v2/pages/experiments/*`（`ExperimentStart` → `ExperimentDetail`，阶段卡片 `StageCard`，结论 verdict + 晋级）、`v2/pages/canary/*`（`RampPlan` 流量爬坡）。
4. **数据中心**：Traces → Pipelines → Datasets（`v2/pages/data/*`），把会话沉淀为数据集。可以直接对应 RL 的 "rollout 数据 / 训练数据集" 管理。

## 10. 值得直接复制的可复用部件

| 部件 | 路径 | 用途 |
|---|---|---|
| V2 外壳 | `src/v2/V2Shell.tsx`、`v2/nav.ts`、`v2/v2.css`、`v2/Logo.tsx`、`v2/Lang.tsx` | 顶栏 + 分组侧栏，整体复用 |
| 组件库 | `src/v2/ui.tsx` | `Button`、`LinkButton`、`Tag`(tone+dot)、`Select`、`FilterSelect`、`SearchInput`、`Segmented`、`PageHeader`、`SubTabs`、`FlowHeader`、`Steps`、`Card`、`Field`、`OptionCard`、`Descriptions`、`Kpi`、`Alert`、`Spin`、`Table`、`Pager`、`Modal`、`Drawer`、`Confirm`、`V2ToastProvider` |
| hooks | `src/v2/hooks.ts` | `useLoad`（丢弃过期响应）、`usePaged`、`useV2Toast` |
| API 客户端 | `src/lib/api.ts` 中的 `request`/`parseResponse`/`ApiError`/`errorMessage`/`pinnedWorkspace` | 统一错误信封和本地化 |
| SSE 解析 | `src/lib/chat.ts` 顶部的 frame parser | POST + 流式响应 |
| 训练 UI | `v2/pages/skilllab/{JobLog,TrainCurve,TrainDetail,TrainWizard,JobList,ArtifactBrowser,state}.tsx` | 日志 tail、reward/score 曲线、作业列表轮询 |
| 阶段进度 | `components/LaunchSequence.tsx`、`StageCard.tsx`、`v2/pages/experiments/StageCard.tsx` | 多阶段作业可视化 |
| 鉴权 | `auth/AuthGate.tsx`、`v2/AuthFrame.tsx`、`backend/app/routers/auth.py`、`core/route_policy.py` | 登录/注册、默认拒绝的路由表 |
| i18n | `src/i18n.ts`、`scripts/i18n_check.py`、`scripts/i18n_zh_punct.py` | en/zh-CN key 一致性和全角标点门禁 |
| 后端骨架 | `core/config.py`（yaml+env 多层配置）、`core/errors.py`、`core/db.py`、`services/aws_clients.py`、`deployer/pipeline.py`（阶段 + resume）、`skill_lab/jobs.py`（队列、子进程、取消、重启清扫）、`skill_lab/artifacts.py`（`read_log` 按 offset 读取、路径防护） | |
| 运维脚本 | `start.py`/`stop.sh`、`scripts/verify.sh`、`scripts/bootstrap.py`、`infra/stacks/base_stack.py` | |

## 11. `design/` 目录

**仓库中没有 `.pen` 文件**：全仓搜索 `*.pen` 结果为空。`design/` 下的内容如下：
- `design/mockup.html`（63 KB）：classic 深色控制台的原始 HTML mockup，是 `theme/tokens.css` 的来源，字体从 Google Fonts 加载 Archivo 和 IBM Plex Mono。
- `design/mockup-observability.html`（35 KB）：可观测模块的 mockup。
- `design/screenshots/`（88 张 PNG）：
  - 顶层：`view_{overview,create,chat,evaluation,governance,registry}.png`、`eval-*.png`、`kb-source-documents-zh.png`、`obs-eval-session-*.png`
  - 子目录：`agent-sdk-caps-fs/`、`eval-pages/`、`final/`（`*-en.png`、`*-zh.png` 成对）、`managed-kb/`、`obs-phase2/`、`obs-phase3/`、`obs-phase4-ux/`

更多 UI 截图在 `docs/lab/images/`、`docs/workshop*/content/static/images/`、`docs/workshop3/static/images/`。这些截图大多是 classic 深色版，例如 `02-deploy-inprogress.png`、`08-run-progress.png`、`09-exp-verdict.png`，可作为进度页和结论页的参考。

## 12. 对 tuningpad 的落地建议

- 直接 fork V2 外壳、`ui.tsx` 和 `v2.css` token，把侧栏分组改成 `构建（环境/Agent）· 数据（任务集/rollout）· 训练（作业/实验）· 评估 · 管理`。
- RL 训练作业照搬 Skill Lab 模式：
  - 后端：`POST /api/train/jobs` 创建；有界队列；子进程或远端作业（SageMaker 等）；`Job` 行 + JSONL 事件；`GET /log?offset=` 增量日志；`GET /train-summary` 中途可读的 step 指标；cancel、resume、`interrupted` 清扫。
  - 前端：2.5s 轮询详情，8s 轮询列表，SVG 曲线显示 reward/score。
- 若需要更细的实时指标，可以在现有 SSE 工具链（fetch + reader）上加 `/jobs/{id}/events` 流。但 launchpad 已证明轮询加 offset tail 足够，而且能跨 nginx/CloudFront 稳定工作。
- 保持以下纪律：所有 boto3 client 走单一工厂；AWS 是事实来源、SQLite 只存标识符；路由默认拒绝鉴权；`?view=` 子页面；en/zh-CN 一致性门禁。
