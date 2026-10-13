# Toolkit 改用固定 Git 来源

> 已获实施批准：用户在父代理呈现精简最终方案后明确回复“开始”。本文件的 R1–R8/AC1–AC8 为实施边界；原规划阶段限制与等待审批说明作为历史记录，不再阻止按正常流程执行本任务。

## 目标与事实

解除默认 sibling checkout 依赖，让后端安装、Agent wheel/模板、Trainer context、OfficeBench helper 使用同一固定 Git 提交，不升级 toolkit 或其他包。

当前安装为 editable sibling，运行路径另由 Settings 决定；消费者需要仓库中的模板、examples、lock、patches，不能只用 wheel 替代。Trainer 现用短 SHA/dirty 摘要且可能纳入 untracked。源码行号、目标本地 HEAD/0.1.5 及 uv 依据全部保留在 [research.md](./research.md) §1–§2。

## 需求

| ID | 最小契约 |
| --- | --- |
| R1 | `backend/pyproject.toml` 的 Git URL + 完整 SHA 是唯一人工权威：`https://github.com/xiehust/agentcore-rl-toolkit.git` @ `7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6`。uv 正常重锁，不新增依赖/升级；Git 安装 direct_url 的 URL+commit 必须一致。 |
| R2 | 小型统一 helper 提供只读 source_spec、只读 inspect、按固定 SHA 返回已验证源码根的 resolve。Agent wheel 与模板共用一次解析结果，uv build 在任务私有副本中执行；Trainer、OfficeBench helper 使用同一 resolver。实际打包仅含 commit tracked 文件，保持 overlay/数据/训练配方。 |
| R3 | 去掉默认 sibling，cache 从 data_dir 派生。有效 cache 获取源码零联网；冷 cache 仅实际消费时获取，网络错误明确失败。import/health/meta/toolkit preflight 不下载；安装与 uv build 的联网另行说明。 |
| R4 | 临时目录+原子发布、本机线程/进程有界锁防部分缓存；Git 非交互、timeout，每次 resolve 最多一次 fetch、无自动重试。检查 commit、必要文件、干净状态及链接 containment；不覆盖/删除未知或用户目录，无 fallback。 |
| R5 | toolkit_path 默认空，保留为可选显式本地供应源，只接受同仓库、同 SHA、干净 checkout；错误 override 不被 cache 掩盖、不 fallback，不修改用户 checkout/真实配置。**dirty 本地开发树及 editable 安装不再满足此契约，须在最终摘要突出。** |
| R6 | toolkit_revision/TrainerImage.toolkit_sha 改为完整固定 SHA，打包核对记录中的预期 revision；沿用现有 profile-revision 标签，新全 SHA 自然区别旧短 SHA/dirty 标签。不扩展任务身份或恢复语义。 |
| R7 | meta/preflight 只读复用现有 DTO/Check 状态，只做必要路径/消息调整。真实消费失败使用最少稳定 AppError 码并补两 locale；更新 README/example/现有来源说明，不新增来源面板。 |
| R8 | 临时 Git fixture/stub 验证所有消费者与失败边界；真实安装准备和离线 unit tests 分开，无 sibling 的隔离副本验证，实施后必须跑 make verify。不移动删除用户 sibling，不进行真实云/镜像构建/训练。 |

## 验收（实施后验证）

- [x] AC1（R1）：无 sibling 隔离安装成功；uv 生成 lock 和 Git direct_url 与权威 URL/完整 SHA 一致，toolkit 仍为 0.1.5，无新增包/升级；文档用 `uv sync --locked` 验证。
- [x] AC2（R2）：fixture 证明 wheel/模板同次解析，Trainer/helper 同源；打包仅 commit tracked 输入，私有 build 不污染 cache，既有 overlay/数据/配方不变；错误安装 metadata 被明确拒绝。
- [x] AC3（R3）：只读诊断/import/health 无 fetch 或 cache 写入；冷 cache 获取一次、热及离线热 cache 零网络，断网冷 cache 明确失败，无 sibling fallback。
- [x] AC4（R4）：线程/进程竞争只发布完整根；锁与静默 Git 超时、失败/中断可安全重试；未知/损坏目录保留、仅清理本次临时目录，fetch 次数有界。
- [x] AC5（R5）：等价 SSH/HTTPS origin 的干净固定 SHA override 可用；错 root/remote/SHA、dirty、非忽略 untracked、缺输入/逃逸链接拒绝，合法内部链接可用，ignored 残留不入包，用户目录不变。
- [x] AC6（R6）：新 Trainer 记录和标签使用完整 SHA，不误复用旧短 SHA/dirty 记录；package_source 拒绝预期 SHA 不符。无排队 payload/Run resume/engine/schema 改动，无旧 runtime 新建 run 禁令或自动云操作。
- [x] AC7（R7）：meta/Check 原形状与 ok/warn/fail 可用；文档明确缓存、安装/build 网络前提及严格本地路径；所有新增错误/必要显示文案双语齐全，无新配置开关或来源面板。
- [x] AC8（R8）：相关 hermetic tests 与最终 `make verify` 通过；安装准备单独记录，unit tests 不访问真实 GitHub/AWS/K8s，不运行云构建/部署/训练。

## 非目标与审批边界

- 不新增 toolkit_offline、toolkit_git_url/revision/cache_dir 配置；不为强制 locked 改 Makefile/start.py/scripts/verify.sh。不实现 Git 对象存储、逐 blob 导出、全树 SHA 清单、多来源下载平台或完整供应链证明。
- 不引入 recipe build_digest/v2 复合标签、全任务 provenance manifest；不修改 Agent/Dataset/Run 排队 payload、Run resume、旧任务恢复规则、engine/DB schema。不顺带修复 Dockerfile 改动不刷新标签的既有策略，不禁止已有 runtime 新建 run，不自动重建/部署云镜像。
- 不改真实配置/用户 checkout，不扩大 OfficeBench 数据、训练配方、auth/network/IRSA 或费用防护范围，不 AWS/CodeBuild/ECR/训练，不安装额外依赖。
- 原规划阶段仅允许任务工件编辑；用户随后已明确批准实施。当前仅修改已批准的来源迁移范围，未扩大到云操作或其他任务。

需求在本文件，技术契约见 [design.md](./design.md)，执行与验证见 [implement.md](./implement.md) 和 [verification.md](./verification.md)，事实与旧方案取舍见 research。AC1–AC8 已按各自定义完成本地/fixture/隔离环境验收，最终 make verify 通过；不把这些证据当云端部署或训练验收。用户随后明确要求重启后端，现已同步 backend/.venv 为固定 Git 安装并仅重启后端，健康检查通过、前端未重启；源码缓存仍按需获取，未提交或归档。
