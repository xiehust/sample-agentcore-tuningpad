# Toolkit Git source — 只读研究汇总

## 0. 证据范围与当前取舍

- 前次规划合并 `dependency-contract`、`source-consumers` 两份只读研究报告，并核读关键本地证据；uv/PyPA 链接来自该报告。以下 Git/uv 命令观察均为前次记录，不是本轮重跑。本次只做范围纠偏，读 AGENTS/workflow/本任务文件与必要源码，不派子代理、不网络/云调用、不安装/测试、不改任务状态。
- 目标为 `https://github.com/xiehust/agentcore-rl-toolkit.git` @ `7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6`。前次只读 `git -C ../agentcore-rl-toolkit rev-parse HEAD` 得到该 SHA，`status --porcelain` 为空，origin 为 `git@github.com:xiehust/agentcore-rl-toolkit.git`；SSH 为同仓库写法，未联网证明远端状态。
- 当前方案：pyproject Git URL+完整 SHA 是唯一人工权威，小型 helper 统一只读声明/检查与按 SHA 获取；Git checkout/干净状态/必要文件检查、tracked-only 私有副本消费，Trainer 仅改完整 SHA。技术契约以 [design.md](./design.md) D1–D6 为准，需求/执行分别在 prd.md/implement.md。
- **旧大方案已被本次取舍替代，以下源码事实不是扩大范围的授权。** 不采用离线开关、复合镜像/配方摘要、全任务 provenance、排队身份/恢复改造、旧任务缺证据禁用、新来源 DTO/Settings 状态机、全树 blob/hash 导出或恶意环境指纹。相关旧建议已改为非目标，不能从研究恢复为实现要求。
- toolkit_path 严格同仓库/同 SHA/干净的兼容收紧仍须最终摘要突出并交用户整体确认；任务仅获准规划，尚未获准实施。

## 1. 安装契约与单一权威

### 1.1 本地事实

| 证据 | 事实与含义 |
| --- | --- |
| `backend/pyproject.toml:20,32-38` | toolkit 未写包版本，来源为 `../../agentcore-rl-toolkit` editable；backend 为 `package=false`，运行时已经依赖仓库布局。 |
| `backend/uv.lock:5-14,29-31,44,2282-2284` | toolkit 版本 0.1.5，editable source 与根 requires-dist 都含 sibling；基础依赖 bedrock-agentcore、starter-toolkit、boto3、python-dotenv。 |
| `../agentcore-rl-toolkit/pyproject.toml:1-10,24-28` | 目标 HEAD 同样为 0.1.5，基础依赖下限与当前 lock 元数据一致；构建依赖 hatchling 未固定。迁移来源不等于升级版本。 |
| `backend/app/core/config.py:15,61-78` | REPO_ROOT 已存在；Settings YAML/env 覆盖只影响构建路径，不影响 uv 的安装来源。当前注释称三端 lockstep，但实际上可分裂。 |
| `backend/.venv/lib/python3.12/site-packages/agentcore_rl_toolkit-0.1.5.dist-info/direct_url.json:1` | 当前仅 `file:///…/agentcore-rl-toolkit` 与 editable=true，没有 commit_id，不能把 0.1.5 或此文件当 SHA 证明。 |
| `Makefile:3-5,13-14`、`start.py:73-76`、`scripts/verify.sh:13-19` | 入口使用未加 locked 的 sync/run，会隐式同步环境；修改 Settings 不会解除安装路径依赖。 |

依赖研究还只读比对了已安装 100 个 distributions 与 lock，未发现版本漂移（额外 lock 项为 colorama 与 virtual 项目）；此数值为历史报告证据，不是本轮重新运行的比对。前次核实 `backend/uv.lock` 为 **553,417 bytes，小于 1 MiB**，不存在可据此声称的 fileEditor 大文件限制。将来锁必须由 uv 正常生成而非手改，这是生成文件契约，不是工具容量障碍。入口未 locked 是现状证据，**不据此要求修改 Makefile/start.py/scripts/verify.sh**，文档验证用 uv sync --locked。

### 1.2 uv 与安装元数据依据

拟采用的官方语法（实施时才修改）：

```toml
[tool.uv.sources]
agentcore-rl-toolkit = { git = "https://github.com/xiehust/agentcore-rl-toolkit.git", rev = "7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6" }
```

- uv Git source/rev 文档：[dependencies.md:331-342](https://github.com/astral-sh/uv/blob/main/docs/concepts/projects/dependencies.md#L331-L342)，公开文档入口：[Git dependencies](https://docs.astral.sh/uv/concepts/projects/dependencies/#git)。`git + rev` 替换 `path + editable`，不并存。
- uv **0.12.23** 官方锁快照：[lock.rs:1058-1064](https://github.com/astral-sh/uv/blob/0.12.23/crates/uv/tests/lock/lock.rs#L1058-L1064)。package source 的形式为 `git = "URL?rev=SHA#SHA"`，根 requires-dist 为 `git = "URL?rev=SHA"`；由 uv 生成并审查，不硬编码锁序列化器。
- [Locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/) 与 [uv sync CLI](https://docs.astral.sh/uv/reference/cli/#uv-sync)：前次本机 `uv --version` 为 0.12.23，只运行 `uv help sync/lock/build`。`--locked` 拒绝过期锁，`--frozen` 跳过匹配检查；`--offline` 只用本地缓存/文件，不能约束应用自己的 Git 命令。
- 授权后的正常流程是改来源 → `uv lock`（保留既有解析偏好，不加全量 upgrade）→ 审查版本/来源差异 → `uv sync --locked`。以后的升级须显式改 rev 后重锁，固定完整 SHA 不因 upgrade-package 变为 main 最新。reinstall-package 仅为安装修复，不是常规必要步骤。
- PyPA [VCS info 数据规范:66-75](https://github.com/pypa/packaging.python.org/blob/main/source/specifications/direct-url-data-structure.rst#L66-L75) 与 [本地目录规则:29-32](https://github.com/pypa/packaging.python.org/blob/main/source/specifications/direct-url.rst#L29-L32)：Git 安装应记录 url、vcs_info.vcs、commit_id；requested_revision 可选。本地目录安装即使位于 Git 仓库也不推断 VCS 信息。

因此可用 Python 3.12 标准库 `tomllib` 从 `REPO_ROOT/backend/pyproject.toml` 读取权威条目，重锁审查 uv.lock，使用前核对 Git direct_url 的 URL+commit。无需新增依赖，不依赖 uv 私有缓存路径。direct_url 只是安装器记录，不能排除恶意篡改或证明外部镜像来源；本任务不实现模块遮蔽/sys.modules 指纹或恶意环境检测框架，不能将 metadata 宣称为完整供应链证明。

## 2. 源码消费者与不扩展的边界

| 消费边界 | 实际 path:line | 现状事实与本次处理 |
| --- | --- | --- |
| Agent wheel/context | `backend/app/services/agents.py:58-79,103-125` | uv build cwd 与模板复制路径都来自 Settings，upload 也附 wheel。改为同次解析的根，私有副本 build，保持 overlay。 |
| Agent pipeline | `backend/app/pipelines/agent.py:73-85,92-113` | context/checks/build/push 分阶段；checks merge 见 `backend/app/pipelines/agent.py:33-40`。保留阶段与 checks 行为，仅必要 helper 接线，不新增 provenance。 |
| Trainer revision/zip | `backend/app/services/trainer_image.py:30-79` | 当前短 SHA+dirty 摘要，Git 未检查退出码；ls-files -co 含 untracked，revision 与打包分离。改完整 SHA、预期 revision 校验、tracked-only 输入。 |
| Trainer selection/source | `backend/app/pipelines/trainer.py:37-80` | 按 region/profile/toolkit_sha 找行，后读当前目录上传。沿用查询与 S3 key 结构，source 打包核对记录的完整 SHA，不增加配方身份。 |
| Trainer image inputs | `trainer_image/Dockerfile:28-38` | 需要完整源码、toolkit 自己的 uv.lock、Megatron patch，不只是 wheel/site-packages；保留环境与 frozen 安装配方。 |
| OfficeBench helper | `backend/app/services/datasets.py:189-195,216-241`、`backend/app/pipelines/dataset.py:77-96` | 动态执行 examples/preprocess.py，当前先 checkout OfficeBench 再载入 helper。helper 改走 resolver，可先验证 toolkit，原数据 checkout/upload 不改。 |
| 模板文件 | `templates/gsm8k_math/template.yaml:8-10`、`templates/officebench/template.yaml:8-9` | 数学 reward/models 与 OfficeBench rl_app/reward/models/utils/tools 都是 toolkit 输入；overlay 仍属于 TuningPad。 |
| 后端直接 import | `backend/app/catalog/models.py:89-94`、`backend/app/services/agents.py:558-562` | _TEMPLATE_HASHES / RolloutClient 来自安装环境；核对 Git direct_url URL+commit 即为本次安装校验边界。 |
| 诊断/API/UI | `backend/app/routers/meta.py:10-19`、`backend/app/services/project.py:199-205`、`frontend/src/lib/api.ts:109-115,133-138`、`frontend/src/pages/Settings.tsx:77-84` | 当前仅 pyproject 存在判断，preflight message 原样显示；复用 Meta/Check 及 ok/warn/fail，只必要路径/消息和双语调整，不新增来源 DTO，不由 GET 下载。 |
| Run 恢复（非改动点） | `backend/app/pipelines/run.py:113-137` | image stage 每次重新 find_or_create 是现状；本次不改变选择/恢复算法，不能影响容量成本确认。 |
| 持久化（非改动点） | `backend/app/jobs/engine.py:123-129`、`backend/app/models.py:91-99,210-216` | ctx.set 持久化 JSON；TrainerImage SHA String(64) 可容完整 SHA。无需 engine/schema 改动，不据此建设任务身份平台。 |
| 排队入口（非改动点） | `backend/app/routers/agents.py:133,175,178-187`、`backend/app/routers/datasets.py:92`、`backend/app/routers/runs.py:196,301` | 现有排队/恢复入口保留；不新增 source identity payload。 |
| 云部署边界 | `backend/app/routers/agents.py:178-200` | rebuild 与 deploy 分离，Git 来源切换不是运行中云镜像更新，不自动操作。 |

`backend/app/services/tools.py:29-39,62-85` 是既有工具下载逻辑，但没有 SHA 源码缓存、跨进程锁、原子目录发布契约，不适合直接复用为仓库缓存。`backend/app/services/agents.py:234-239` 中 `_run()` 的超时依赖输出到来，不适合新 Git wrapper；不借本任务重构全部 subprocess。

重要非目标：OfficeBench 自己的 upstream clone/main（`backend/app/services/datasets.py:199-213`，以及 `templates/officebench/agent/Dockerfile:11-14`）不是 toolkit helper 来源；保持行为，但单元测试也必须 stub 该网络。

## 3. 当前取舍与撤回的扩展

| 候选 | 本次取舍（替代旧建议） |
| --- | --- |
| pyproject source 与独立 Settings URL/SHA | 选前者；后者重现 config.py 与 uv 双来源分裂。tomllib 足够，已有 REPO_ROOT。 |
| uv 私有 cache / site-packages 当源码根 | 不采用：私有结构不稳定，wheel 不保证 examples/lock/patch；应用按 SHA 缓存普通 Git checkout，不自建对象存储/逐 blob 导出/全树 SHA 清单。 |
| 任意 toolkit_path 开发覆盖 | 不采用；保留同仓库、同 SHA、干净的显式本地供应，后端仍 Git 安装；无 editable/dirty 模式。开发体验收紧待整体确认。 |
| 新配置开关 | 非目标：不新增 toolkit_offline、toolkit_git_url/revision/cache_dir。仅 toolkit_path 可空，cache 派生自 data_dir；有效 cache 不联网、冷 cache 网络错误失败，安装/build 联网独立说明。 |
| 启动自动 clone | 不采用；仅实际消费 resolve。uv run 可能安装依赖，不能承诺整个 start.py 永不联网。 |
| 新 Trainer DB 字段、配方摘要/v2 复合标签 | 非目标：保留现有字段/查询/tag 结构，toolkit_sha 改完整 SHA，打包核对预期 revision。完整 SHA 自然隔离旧短 SHA/dirty 标签。 |
| Dockerfile/buildspec 改动不刷新标签 | 既有策略限制，非本次换源要修复的问题；不加 recipe build_digest、S3 复合 key 或 image_uri 新查询策略，不建设全任务 provenance manifest。 |
| 新来源 DTO / 全包环境指纹 | 非目标：复用 Meta/Check，少量必要文案/type 调整即可；Git direct_url 校验不是完整供应链证明。 |

**补充核查：symlink 不能一概拒绝。** 前次 `git ls-tree -r HEAD` 发现两个 120000 blob：`../agentcore-rl-toolkit/CLAUDE.md:1` → `AGENTS.md`；`../agentcore-rl-toolkit/.claude/skills/check-cloudwatch-session-logs/SKILL.md:1` → `../../../.agents/skills/check-cloudwatch-session-logs/SKILL.md`。目标都属于同一提交树。拟允许可证明位于树内的相对 symlink，拒绝逃逸、绝对路径、环、缺失目标与 submodule；不能仅因为路径含 `..` 就拒绝合法相对链接。消费 zip 时只安全展开同提交目标。

**补充核查：同仓库不等于字面 URL 相等。** 现有 origin 是 GitHub SCP-like SSH 写法；显式本地来源校验只针对此仓库规范化 SSH/HTTPS 身份，不改写用户 remote、不发 SSH 请求。自动获取仍只使用 pyproject 的 HTTPS URL。

**撤回的恢复建议（明确非目标）：** 不实现新任务排队身份、跨 stage provenance manifest、Run resume 继承改造或“旧任务缺身份就禁用/要求重建”政策；Agent/Dataset/Run payload、旧任务恢复规则、engine/DB schema 保持。Trainer 仅在真正打包时核对现有记录的预期完整 SHA，不猜测短 SHA、不迁移旧行。不禁止已有 runtime 新建 run，不自动重建/部署云镜像。

## 4. 测试、风险与验证记录

- sibling 泄漏：`backend/tests/test_agents_datasets.py:34-51` 只 stub wheel，仍读真模板；`backend/tests/test_agents_datasets.py:88-99` 直接读 sibling Python 文件。改为独立 fixture + 本仓库 overlay 安全检查，真实固定源码的集成检查另列，不能以合成 fixture 冒充上游安全证明。
- 测试隔离：`backend/tests/conftest.py:16-35` 只隔离 data/config 与 AWS/K8s，不拦 Git/一般网络；须补默认拒绝 toolkit fetch/build、隔离相关 env、需要时显式注入受控本地仓库。
- 既有回归/错误参考：`backend/tests/test_engine.py:41-59,74-90`；模型 registry 参考 `backend/tests/test_catalog.py:156-171`；run 镜像目前仅 stub（`backend/tests/test_runs.py:437-449`）；meta/auth 参考 `backend/tests/test_core.py:12-39`。保留这些现状证据，不要求任务身份/恢复改造；新增 Trainer full SHA 选择断言放对应测试。行号为文件检查记录，不表示执行测试。
- 新测试仅覆盖：临时 Git 两提交与 direct_url stub、无 sibling、冷热/断网 cache、错误 override/损坏/未知目录、dirty/untracked/ignored、合法与逃逸链接、线程/进程原子发布、超时/失败清理、preflight 不下载、完整 SHA 不误复用旧 tag、预期 revision 拒绝不符、同源 wheel/模板/Trainer/helper 及私有 build 不污染 cache。模块遮蔽框架、任务身份/恢复平台测试是撤回的非目标。GitHub/AWS/K8s/CodeBuild/ECR/Docker build 均不真实调用。
- 离线有两种 cache：uv 依赖/构建 cache 与应用源码 cache。只有 lock、只有安装包、或只有源码 cache 都不保证全流程离线；Agent wheel 的 hatchling 和 Trainer/OfficeBench 的其他网络输入仍有独立前提。固定源码 SHA 不保证完整镜像字节可复现。
- 前次规划仅做文件/只读 Git/uv help/引用检查，未安装或测试；本轮仅文件/引用检查及任务工件有界编辑，未调用 uv lock/sync/build/help、clone/fetch、服务、测试或云。`make verify` 留待实施后必须执行，本轮不声称通过。
- 本次重新核读 trainer service/pipeline、config、meta/project、前端 Meta/Check/Settings，确认完整 SHA 字段与既有 DTO 足够最小接线；未改变上述源码。真实目标拉取及锁解析留待授权后验证，失败不能换分支。
- 只写本任务四份 Markdown 和两个必要 JSONL；task.json 保持 planning，旧任务/docs 不动。最终只读校验记于 implement.md §4；父代理呈现最新精简摘要后仍需用户明确批准。
