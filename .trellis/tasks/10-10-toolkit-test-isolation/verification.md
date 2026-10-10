# Toolkit 测试隔离验收 — 2026-10-10

## 修改范围

仅修改 sibling toolkit 的 `tests/test_rollout_entrypoint.py`，生产源码与依赖未改：

- 函数级 autouse fixture 在 app 构造前替换 boto3 的 S3 工厂，每个用例得到新的受限 mock。
- 去掉构造后才赋 mock 的路径；增加构造即获得 mock 的回归断言。
- 结果地址用例等待后台保存，检查 mock 写入的 bucket/key/body 与响应和原始 payload 一致。
- 保存 helper 在 TestClient 上下文中检查写入发生且活动任务归零，轮询截止时间为 5 秒；增加 handler 抛错与非 dict 返回值的 500 结果断言。

## 验证结果

本地验证器 `.run/toolkit-offline-pytest.py` 在导入测试前清除本进程的 AWS 环境影响，将 AWS 配置/凭据文件指向空设备并禁用 metadata；拦截真实 boto3 客户端构造、botocore API 调用及 Python 审计事件中的外部 DNS/socket 访问。违规在抛异常前记录，pytest 后统一检查，因此后台吞掉异常也不会让验证成功。

| 检查 | 结果 |
| --- | --- |
| 验证器自检 | AWS 客户端、外部 DNS 和外部 socket 探针均在真实调用前被拦截 |
| 修复前基线 | 原结果地址用例按预期失败：构造 app 时尝试创建真实 S3 客户端；未联网 |
| 修改后的目标文件 | 21 passed；`denied_attempts=[]` |
| 目标文件 + 同步/异步 client 测试 | 75 passed，包含上述 21 个；`denied_attempts=[]` |
| Ruff check / format --check | PASS，使用 TuningPad 已有 Ruff 0.14.0 与 toolkit 的 pyproject 配置 |
| 独立只读审阅 / git diff --check | 无需修复项 / PASS |
| TuningPad `make verify` | PASS |

在 toolkit 目录运行的关键命令：

```bash
uv run --no-sync --offline python ../sample-agentcore-tuningpad/.run/toolkit-offline-pytest.py --self-test
uv run --no-sync --offline python ../sample-agentcore-tuningpad/.run/toolkit-offline-pytest.py tests/test_rollout_entrypoint.py tests/test_client.py tests/test_async_client.py -q --maxfail=1
```

未安装新依赖，没有运行未经审查的完整 toolkit 集成套件。SDK 弃用警告和既有 Ruff 配置弃用警告未在本批扩展处理。

## 验证边界与交付

- 这是进程内 Python 审计和 AWS 调用拦截，不是 OS 网络沙箱，也不宣称隔离任意子进程；证据仅覆盖上述实际执行的测试。
- 5 秒限制覆盖后台轮询，不覆盖整个 HTTP 请求或 TestClient 退出。底层 SDK 的空闲 daemon loop 生命周期未改，涉及 S3 的用例已等待其跟踪任务结束。
- toolkit 提交：`99ab5d7`（`test(app): isolate rollout entrypoint aws clients`）。已推送 `origin/fix/rollout-test-isolation`，尚未创建或合并 PR，toolkit `main` 未改。
- 本子任务 AC1–AC4 的实现验收通过，状态保留 `in_progress` 等待合并收尾；父任务只关闭 AC2 的修复/离线证据项，不关闭任何真实训练验收。
- 未执行 AWS/K8s 操作、镜像构建或 GPU 验收；原 rollout failure policy AC5、镜像更新核实与其余历史验证缺口继续开放。
