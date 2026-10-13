# Toolkit Git source — 拟议设计

> 用户已在精简最终方案后回复“开始”，批准按此设计实施。需求见 prd.md，事实与行号见 research.md；旧方案的扩展仍为非目标。

## D1 — 权威与安装（R1）

`backend/pyproject.toml` 的 uv source 是唯一人工 URL/rev（值见 PRD R1），以 Git+完整 SHA 替换 path/editable。`uv lock` 正常生成派生锁，不加 upgrade、不新增依赖；审查版本/来源差异后用 `uv sync --locked` 验证，不改 Makefile/start.py/scripts/verify.sh 来强制 locked。

小型 `services/toolkit_source.py` 用标准库 tomllib 读取声明。安装校验只需 Git direct_url 的 URL、vcs=git、commit_id 与权威一致，缺失/file/editable/不匹配则明确失败，不在请求中安装。direct_url 是安装器记录，不是完整供应链或恶意环境证明；不做全包/sys.modules 指纹。

## D2 — helper 与配置（R2/R3/R5）

- `source_spec()`：只读返回 URL/完整 revision；无目录创建、无网络。
- `inspect_source()`：只读检查安装 metadata、显式路径或 cache，返回内部结果供原 DTO 使用；本地 Git 也限时，不 clone、不修复。
- `resolve_source(expected_revision=None)`：核对权威/安装及可选预期 SHA，返回 `{root, revision}` 已验证源码根；消费者不再各自读 toolkit_path。校验/tracked-copy 是 helper 内部小函数，不形成公共来源 DTO。

`toolkit_path: Path | None = None`；非空先校验，哪怕已有热 cache 也不能掩盖坏 override。只读接受同仓库 origin（限定本仓库 SSH/HTTPS 等价形式）、固定 HEAD、干净完整的真正 worktree root，不 checkout/reset/clean 用户目录；相对路径仍按 cwd 解释，文档推荐绝对路径。有效本地供应可直接返回已验证根，消费前后复检，复制/build 均在任务私有目录。

默认 cache 为 `<data_dir>/cache/toolkit/<full_sha>/`，校验 origin 防误用；无 sibling 查找，无独立 URL/revision/cache/offline 设置。有效 cache/本地源不联网；冷 cache 网络失败明确报错。安装 uv sync/run 与 uv build 可能下载 Git 包/hatchling 等，不受源码 cache 命中保证；全流程离线还需准备各自 uv cache。

## D3 — 缓存、Git 与文件安全（R4/R5）

1. 同 SHA 用本机 `threading.Lock` + POSIX `flock` 有界串行化，锁内复检 cache；持久锁文件不 unlink。有效根复用，未知/损坏目标报错保留，不覆盖或自动修复。
2. 冷 cache 在同文件系统自建临时目录初始化普通 Git checkout，一次 fetch 固定完整 SHA，detached checkout 后验证再原子 rename 发布；不切 main/tag、不递归拉 submodule。中断不暴露半成品，只清理本次创建的临时目录，其他目录不扫删。
3. 验证 origin、HEAD commit、干净状态及必要 pyproject/README/src/uv.lock/模板/examples/patches；拒绝 staged/unstaged 和非忽略 untracked。用 Git 的 commit tracked 路径枚举复制，排除 .git 与 ignored 构建残留；复制前后复检 SHA/干净状态。不自行实现对象库、逐 blob cat-file 导出或全树 SHA manifest。
4. 路径及链接目标必须留在同一源码树且属于 tracked 输入；允许合法内部相对 symlink（research §3 的两个实例），不因含 `..` 一概拒绝。拒绝绝对/逃逸/循环/缺失目标、submodule；zip 仅安全展开内部 tracked 目标。cache 与用户 checkout 都不执行 build/helper 写入。
5. Git argv 不经 shell、stdin DEVNULL、关闭提示/askpass/credential helper，隔离会改变仓库/URL 的 Git 环境和全局配置、禁 hooks，保留 TLS 校验；本地只读检查禁 optional locks。检查所有返回码，不复用 agents._run 的输出驱动超时。拟定锁 60s、Git 单命令 120s、resolve 总 300s、inspect 总 10s；超时回收子进程，诊断截断且去敏。一次 fetch、零自动重试；不新增调参开关或通用 subprocess 框架。

## D4 — 消费者与 Trainer 接线（R2/R6）

- Agent 准备 context 时 resolve 一次；模板复制和附带 wheel（含 upload 流程）共用该根，tracked 私有副本中运行 uv build，保持 wheel 合同、overlay 顺序与 static checks。OfficeBench helper 也从 resolver 根私有复制后载入，避免 pycache 污染；保持原 upstream 数据流程，GSM8K/upload 不无谓获取源码。
- 后端现有 toolkit import 使用点可局部复用安装 metadata 校验，不下载源码、不创建额外 AWS client。所有消费者只接 helper，不新增来源记录系统。
- `toolkit_revision()` 只读返回权威完整 SHA；沿用 TrainerImage.toolkit_sha、region/profile 查询、`{profile}-{revision}` 标签及原 S3 key 形式，完整 SHA 自然区别旧标签。不回填旧行，不加入配方摘要或 image_uri 新查询策略。
- Trainer source stage 传已有记录的预期 revision，package_source 在上传前核对完整 SHA 并用 resolver 的 tracked 输入打包，保留 Dockerfile/buildspec/toolkit lock/patch。预期不符明确失败，不猜短 SHA；只做此消费边界校验，不增加旧任务恢复分支。镜像选择不 fetch；必要 pipeline 改动仅接 helper/传预期 SHA。

非目标：Agent/Dataset/Run payload、Run resume、旧任务恢复规则、engine/DB schema、全任务 provenance、recipe build_digest/v2 复合标签均不改。Dockerfile 变化不刷新同 SHA 标签的既有策略留待别项；不禁止已有 runtime 新建 run，不自动 rebuild/deploy。

## D5 — 只读诊断与最少错误（R3/R7）

meta 保持 `toolkit_path: string`（显式路径或派生 cache 路径）与 `toolkit_present: boolean`（源码已验证）；Check 保持 id/status/message/detail。安装一致且源码有效 → ok；未缓存 → warn/首次使用获取；安装/override/cache 错误 → fail。inspect 不探测网络可达性，不能把未缓存猜成“离线”；不 fetch/建 cache，不新增公共来源 DTO 或 Settings 状态机/详情面板。

真实使用才 raise：优先复用现有错误，最少区分来源/安装不符、获取/本地 IO 失败、锁/Git 超时（候选 `toolkit.source_invalid`、`toolkit.source_unavailable`、`toolkit.source_timeout`）。安全 detail 给 expected/actual SHA 或简短原因，不暴露任意 stderr/凭据；实际新增码须在两 locale 配对。正常 toolkit 检查行若需本地化，按既有 id+status 与路径使用少量 t() 文案；无新字段为默认方案，必要局部显示/type 调整说明理由。

import、health、无任务恢复的启动也不预取；恢复中实际消费仍可 resolve。仅 toolkit 检查只读，不宣称整个 project preflight 没有既有 AWS/ledger 副作用。

## D6 — 兼容、范围与回滚（R5/R7）

**最终审批摘要必须突出：toolkit_path 不再是 dirty/异版开发 override，后端不再 editable；只改 sibling 文件不能被新构建采用。** 用户 checkout 无需移动/删除，真实 config 不自动改；坏路径须用户修正或取消设置。无 toolkit_offline 或平行 Git/cache 配置。

授权后成对改 pyproject/lock、同步并在隔离环境验证；未来用户请求 Trainer 时全 SHA 新标签可能走既有收费构建流程，但本任务不执行云操作、不更新现有 runtime。回滚为人工恢复已验证应用+pyproject/lock 配对、另行同步环境，不做自动来源 fallback 或云回滚。

限制：本机 POSIX 缓存锁，不保证 NFS/多机；固定源码不锁 base image/hatchling/OfficeBench 数据，不证明整镜像可复现。目标远端获取与真实重锁尚待授权后验证；若要求新增依赖/升级/扩大接口或恢复政策，先返回规划。

预计实施文件限于依赖/锁、config/helper、现有源码消费者与必要 pipeline 接线、meta/project、相关测试/两 locale、README/example/既有来源说明；普通显示确需时才微调 Settings/api 类型。具体顺序与检查见 [implement.md](./implement.md)，不是本轮可写权限。
