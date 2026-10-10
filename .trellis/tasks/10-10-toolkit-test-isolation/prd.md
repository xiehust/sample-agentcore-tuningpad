# 隔离 toolkit rollout 入口测试的 AWS 客户端

## Goal

仅修复 sibling toolkit 的 rollout 入口测试隔离：构造前 mock S3、验证后台写入，并在阻断外部网络的环境中回归。

## Requirements

- **R1 构造前隔离**：在 `../agentcore-rl-toolkit/tests/test_rollout_entrypoint.py` 内使用测试级 fixture，在每次构造 `AgentCoreRLApp` 之前替换其 S3 客户端工厂。不能依赖主机凭据、AWS 配置或事后给 `app.s3_client` 赋值。
- **R2 后台结果可验证**：现有 `test_response_includes_result_location_with_rollout_config` 不只断言 HTTP 返回地址，也应有界等待后台结果并检查 mock 收到的 bucket/key/body。后台线程的异常不能被 HTTP 200 掩盖。
- **R3 回归隔离**：用独立 mock 实例避免用例间状态泄漏；保留现有成功/错误结果保存、session id、缺字段和非法返回值测试的语义。阻断外部网络并清除真实凭据影响后执行回归，网络违规须成为可见失败。
- **R4 最小范围**：产品代码 `src/agentcore_rl_toolkit/app.py` 不改；预计只修改上述测试文件，复用现有 pytest/monkeypatch/MagicMock。若发现必须修改生产代码、增加依赖或扩大全套测试 fixture，先停下来说明。

## Evidence

- `src/agentcore_rl_toolkit/app.py:40-44`：构造函数立即执行 `boto3.client("s3")`；`:94-105`：保存结果会执行 `put_object`。
- `tests/test_rollout_entrypoint.py:91-117`：配置了真实 S3 保存路径，但未替换 S3 客户端，也未等待后台写入。
- 同文件 `:274-281`：辅助函数在构造 app 后才赋 mock，因此构造阶段仍会访问真实 AWS 客户端初始化链。
- `tests/test_client.py:358` 等现有测试在构造前 patch boto3，可沿用测试替换方式；该仓库没有根级 `tests/conftest.py`。
- 本地曾记录真实 S3 `AccessDenied`；本轮仅读代码确认路径，没有重跑未隔离测试来触碰 AWS。
- 所有 toolkit 路径相对于 sibling checkout；遵守其 `AGENTS.md` 与 `CONTRIBUTING.md`。`AGENTS.md` 超出自动上下文注入上限，实施前须直接分段读取原文件（尤其 Testing / Development Tips），不依赖截断注入；本任务未修改注入限制。

## Acceptance Criteria

- [ ] AC1：目标测试文件中的 app 构造均使用新建的 mock S3 客户端，不创建真实 AWS 客户端，不依赖本机凭据或 metadata service。
- [ ] AC2：完整 `_rollout` 配置的后台保存被等待并断言，HTTP 返回地址和实际 mock 保存 key/body 相符；成功结果与异常结果回归均通过。
- [ ] AC3：在阻断外部网络的验证进程中运行该文件，测试通过且外部连接尝试为零；验证工具不能只吞掉线程异常。
- [ ] AC4：目标文件 Ruff 检查及相关离线回归通过；没有生产代码、依赖、凭据或云资源变更。提交/push 前再次检查 sibling 工作区及分支，不覆盖其他工作。

## Out of Scope

- 不修改 toolkit 应用 API、结果契约、rollout failure policy 或生产网络逻辑。
- 不运行真实 AWS/K8s 测试、CodeBuild、镜像重建或 GPU 验收，不读取或修改凭据。
- 不把此项通过当作原 rollout failure policy AC5 已通过，不改变 OfficeBench 配方“未验证”标签。

## Planning Status

用户已要求提交首批并继续其余事项。本子任务是下一批无云费用的最小方案，仅涉及一个测试文件，采用 PRD-only 规划；最终摘要确认前保持 `planning`，未修改 sibling 文件。

验证使用现有 uv 环境与 pytest；先执行目标文件的断网回归，再检查相邻 client 测试是否可安全纳入。完整 toolkit 测试中可能有主动联网的集成用例，未审查前不直接全量运行。
