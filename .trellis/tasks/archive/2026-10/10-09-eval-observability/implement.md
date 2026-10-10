# Implement Plan: 评估结果可观测性

按顺序推进，每一步结束都跑对应测试；第 0 步和第 7 步涉及真实 AWS，开始前单独确认费用。

| # | 内容 | 主要文件 | 验证 |
|---|---|---|---|
| 0 | 真实环境探针（spike，约 $1–3）：给 OfficeBench 镜像加 ADOT 后部署一个 `_obs` runtime，开启 us-east-1 的 Transaction Search（需确认），跑 1–2 个样本，记录 span 实际落点、内容位置、AgentCore 注入的 OTEL 变量、摄入延迟、冷启动变化；结果写回 design.md §6 并据此修正 §2/§4 | `templates/*/agent/Dockerfile`、`tp_entry.sh` | 实测记录 |
| 1 | 样本接口（R1）：results.jsonl 读取与缓存、状态判定（与 `summarize_eval` 共用规则）、对话规范化、脱敏、任务输入 | `services/serving.py`、`routers/serving.py` | 单测：计数一致、脱敏（含 `api_key`、`tp-` key）、截断、GSM8K 无对话、失败行 |
| 2 | 前端样本列表与对话页（R1） | `pages/Evals.tsx`、`pages/EvalDetail.tsx`、`components/Transcript.tsx`、`lib/api.ts`、`app.css`、两个 locale | typecheck、lint、i18n `--strict`；本地 mock 浏览器检查（en / zh-CN） |
| 3 | Transaction Search 状态与开启（R2） | `services/observability.py`、`routers/observability.py`、`main.py` 注册路由、preflight `REQUIRED_ACTIONS` | 单测：未确认不写、已开启不重复写、策略内容与条件 |
| 4 | 评测专用 runtime（R2）：`AgentRuntime.obs`、`Eval` 新列、`stage_observability`、`deploy_runtime` 支持 `environmentVariables`、清理路径 | `models.py`、`services/agents.py`、`pipelines/serving.py`、`pipelines/agent.py`、`pipelines/cluster.py` | 单测：训练 runtime 参数不变、`_obs` 创建 / 复用 / 镜像变化时更新、删除时一并清理 |
| 5 | 调用链接口（R3）：Logs Insights 查询、span 规范化与建树、pending / empty、缓存 | `services/observability.py`、`routers/serving.py` | 单测：用文档中的 Strands span 样例与第 0 步采到的真实样例做 fixture，校验父子、深度、offset/width、类别、token、内容解析、脱敏 |
| 6 | 前端瀑布流与新建表单开关、Transaction Search 确认框（R2/R3） | `components/TraceWaterfall.tsx`、`pages/EvalDetail.tsx`、`pages/Evals.tsx`、`lib/api.ts`、`app.css`、locale | typecheck、lint、i18n；mock 浏览器检查 ready / pending / empty / 错误四种状态 |
| 7 | 真实环境验收（AC4，约 $2–5，需批准）：重建两个模板 agent 镜像，在一个集群上开 g5 推理端点，跑开启调用链的 OfficeBench 评测（≤ 11 样本），截图瀑布流；确认训练 runtime 没有 span；结束后删除端点、节点组缩回 0 | — | 截图 + 记录 |
| 8 | `make verify`、更新 `.trellis/spec`（可观测性与评测接口约定）、提交 | — | `make verify` 通过 |

风险点与回滚
- `services/agents.py:deploy_runtime` 被训练与评测共用：改动只增加一个默认为空的 `environment` 参数，并用测试锁住训练路径不传它。
- 镜像改动影响所有新构建的 agent：入口脚本在未设置 `TP_OBSERVABILITY` 时直接 `exec python -m rl_app`，行为与现在一致；有问题时把 Dockerfile 回退即可。
- Transaction Search 是账号级写操作：只在用户确认后调用，代码里没有任何自动开启路径。
