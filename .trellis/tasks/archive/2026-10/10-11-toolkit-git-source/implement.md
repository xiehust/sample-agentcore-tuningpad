# Toolkit Git source — 后续实施计划

> 用户已在精简最终方案后明确回复“开始”。可以启动本任务并执行下列清单；具体测试/命令结果另记 verification.md，未执行项仍不得声称通过。依赖安装与最终验证优先使用独立临时环境，不热替换已有后端环境、不重启真实服务。

## 开工门槛

- [x] 父代理呈现精简摘要并取得用户“开始”批准；严格 toolkit_path 对 dirty/非 Git 安装开发的影响已入 README/example/spec。重读工件/指南，保护工作树；历史规划禁令已由明确实施批准取代。
- [x] 固定 URL/SHA 不变，不加依赖/升级；安装准备与离线测试分开，云操作仍未执行。无扩大接口、配置或恢复政策。

## 1. 串行步骤

以下完成项结合独立审阅、现有源码及本节点全回归证据；详见 verification.md。
任务仍为 `in_progress`，最终整体验收及 `make verify` 留给父代理。

### S1 — 依赖（R1；D1；AC1）
- [x] pyproject 替换 Git source，uv 正常重锁；102 个 lock 包版本无新增/升级。无 sibling 副本 `uv sync --locked --python /usr/bin/python3.12` 成功，toolkit 0.1.5、direct_url URL/完整 commit 和 venv 内模块路径一致；未改标准入口脚本。

### S2 — helper（R2–R5；D2/D3；AC2–AC5）
- [x] 实现只读 source_spec/inspect、resolve、Git checkout/干净状态/必要文件检查与 tracked-copy；toolkit_path 默认空，cache 从 data_dir 派生；坏 override 不 fallback。
- [x] 有界线程/进程锁、临时目录原子发布、非交互 Git/timeout/单次 fetch；保留未知目录和用户目录。临时 Git/metadata stub 覆盖合法内部 symlink 与逃逸；无对象/全树 hash 平台。另完成真实 Git 冷 cache 准备与禁 fetch 热 cache/copy 验证。

### S3 — 消费者（R2；D1/D4；AC2）
- [x] Agent wheel/模板复用同次 resolve，任务私有副本 uv build；OfficeBench helper 私有载入，保持数据/overlay/静态检查。后端相关 import 只接安装 metadata 校验，不克隆；consumer tests 通过，未执行真实云构建。

### S4 — Trainer（R2/R6；D4；AC2/AC6）
- [x] toolkit_revision/TrainerImage.toolkit_sha 用完整 SHA；package_source 接预期 revision，上传前核对并 tracked-only 打包。沿用 profile-revision 标签及查询/S3 key 结构，只必要 pipeline 接线；不改配方、任务 payload/恢复规则、engine/schema。

### S5 — 诊断/文档（R3/R5/R7；D5/D6；AC7）
- [x] meta/preflight 只读复用 DTO/Check；最少 AppError 及两 locale、普通显示文案局部调整，无新面板/状态机。i18n strict、frontend lint/typecheck/build 通过；S5 mock SSR 不冒充浏览器验收。
- [x] README/example/AGENTS/现有 spec 与来源注释修正 sibling 契约，说明严格路径、uv 与源码 cache、安装/build 网络及不自动云更新；未动真实配置或其他文档任务。

### S6 — 验证与审查（R8；AC8）
- [x] 独立审阅 P2 已实证复现并最小修复：仅为 Run training-plan 测试补有效 ModelCheck stub，保留校验/费用断言及离线保护，未改生产验证顺序。
- [x] 无 sibling 隔离副本全 backend pytest：311 通过；Ruff check/format、frontend lint/typecheck/build、i18n strict 通过。独立 venv/config/data，未移动/删除/挂载用户 sibling，未加 deps。
- [x] 父代理在无 sibling 的 Python 3.12 隔离副本执行完整 `make verify`（UV_OFFLINE/HF_HUB_OFFLINE/禁 metadata 环境），退出 0、全部分节 OK、最终 PASS；AC8 验收完成，具体环境与证据见 verification.md。该验收阶段未同步原 backend/.venv 或重启服务；之后按用户明确要求完成了实际环境同步与后端重启，见 verification.md 的后续激活记录。
- [x] 审查 diff 无非目标、依赖升级、真实配置/数据及 auth/cost/network 边界变化；148 项源文件和测试副本哈希一致。报告实际结果。未实施回滚、删除用户目录、云操作、commit/archive/journal。

## 2. Hermetic 测试矩阵

| 测试边界（新文件为候选） | 关键断言 | 验收 |
| --- | --- | --- |
| test_toolkit_source.py | 临时两 commit Git 与 metadata stub：权威/direct_url 一致及不符；冷/热/断网 cache、坏 override/必要文件/dirty/untracked/ignored、无 sibling；合法内部链接与逃逸。 | AC1–AC5 |
| 同 helper 并发/失败 | 线程+多进程以 barrier/events 控制，完整发布一次；锁/静默 Git 超时、fetch 失败及中断；不覆盖坏/未知目录，仅清理本次临时目录，重试无死锁。 | AC3/AC4 |
| test_agents_datasets.py + catalog | stub uv build，wheel/模板同根同次解析、私有 cwd；Trainer/helper 同 resolver，overlay/数据/静态检查保持，cache 无 build/pycache。 | AC2 |
| test_trainer_image.py | full SHA/tag/记录查询区分旧短 SHA/dirty；zip 仅 tracked，预期 SHA 不符在上传前失败，StubClient 验证云参数，不真实构建。 | AC2/AC6 |
| test_core.py/test_setup.py + locale | meta/health/inspect/preflight toolkit 无 fetch/写 cache，原 DTO/Check 状态、错误双语；不新增来源 API。 | AC3/AC7 |
| 隔离安装/全回归 | 独立 parent 无 sibling、独立 venv/config/data；真实 direct_url 核验另记；既有 Run/engine 回归保持，不新增身份/恢复测试要求。 | AC1/AC6/AC8 |

conftest 默认拒绝真实 toolkit fetch/build，临时 Git 测试显式 stub 远端；隔离 toolkit_path/env，AWS/K8s/Hugging Face/OfficeBench 网络均拒绝或 stub。fixture 组装与本仓库 overlay 安全分开，不将合成 fixture 当上游源码安全证明。

## 3. 原规划验证命令（历史模板；实际结果/临时环境见 verification.md）

```bash
# 安装准备：获批后独立环境，可按需访问公开 Git/包索引；无云
cd backend
uv lock                 # 审查来源及逐包版本差异后才继续
uv sync --locked
# 准备完成后离线 unit tests（含新增文件及现有回归）
uv run --locked --offline pytest -q
cd ..
python3 scripts/i18n_check.py --strict
UV_OFFLINE=1 make verify
```

make verify 必须最终执行；缺依赖 cache 先报告环境缺口，另行准备，不让 pytest 临时联网。UV_OFFLINE 只约束 uv，普通 Git/网络仍由 hermetic stubs 拦截。无 AWS credentials/元数据访问，不 start.py，不 Docker/CodeBuild/ECR/训练。搜索残余必需 sibling 文案；若有 UI 微调仅 mock API 双语检查，无新增测试依赖。

## 4. 规划阶段核查（历史记录；非当前实施状态）

- 已复读精简工件：R1–R8/AC1–AC8、D1–D6、S1–S6 对应；旧扩展仅作为非目标，源码证据保留在 research。
- 只读结构检查通过：JSONL 10/9 条合法、无重复且路径存在；5 个相对 Markdown 链接有效，44 个本地源码引用存在且行号未越界。task.json 哈希与开始一致、仍 planning；tracked/staged diff 为空，未跟踪目录列表不变。
- 只编辑本任务六个允许工件；未安装/测试/网络/云调用。make verify 尚未运行，留待获批实施后；下一步仅交父代理呈现最新摘要，等待用户确认。
