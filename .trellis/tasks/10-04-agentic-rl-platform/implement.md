# Implement Plan

开发分阶段交付，每个阶段结束都要跑一次 `make verify`。凡是会在真实 AWS 上产生费用的验证，都要先单独取得用户批准。

| # | 阶段 | 产出 | 验证 |
|---|---|---|---|
| P1 | 骨架 | backend 基础：config、db、errors、auth、aws/k8s 工厂、作业引擎，以及对应测试。frontend：移植 V2 外壳、ui.tsx、css、hooks、api、i18n，页面路由先做成空壳。另外提供 start.py、stop.sh、Makefile | `make verify`。本机启动后能看到侧栏和登录页 |
| P2 | 项目 Setup 与目录 | preflight（配额、权限、kubectl/helm）、S3 和 ACR 执行角色；模型兼容性检查；P 系列机型目录（规格、价格、配额）；资源规划器 | 单测（规划器表驱动）。真实环境验证：HF 元数据、配额、价格、setup 可重复执行 |
| P3 | 集群管理 | CFN 新建向导和导入流程、平台组件安装器（access entry、ns、Pod Identity、KubeRay、LBC、FSx PVC、SG、guardian）、实例组和节点管理、training plan 查询与购买（需确认）、删除集群 | 单测（渲染、阶段逻辑打桩）。真实环境：`cfn validate-template` 和参数预览，training plan 报价查询。**E2E-a（需批准）**：新建只有 system 组的集群，并装好全部组件 |
| P4 | Agents 与数据集 | 模板（gsm8k_math、officebench）、上传/镜像导入、agent 流水线（VPC 模式 runtime）、数据集上传/转换/内置生成/预览 | 单测。E2E-a 集群上部署 GSM8K agent 并冒烟。OfficeBench 数据集生成到平台桶 |
| P5 | 训练 | trainer 镜像（CodeBuild，fsdp/megatron）、RayJob 和训练命令渲染器、run 状态机（扩容、数据准备、提交、监控、重试、停止/续训、缩容）、日志解析和指标、向导与详情页 | 单测：渲染快照对照仓库验证过的脚本，日志解析，状态机打桩。trainer 镜像真实构建（CodeBuild，花费很少） |
| P6 | 导出、推理、评估 | merge Job、vLLM Deployment 加 NLB、评估作业和对比视图 | 单测（渲染、汇总） |
| P7 | 资源与成本、概览 | 资源汇总与清理、仪表盘 | 单测。真实环境列出资源 |
| E2E-b | GPU 冒烟（需批准） | 扩容 1×p5（或 p4d）→ GSM8K 训练 2 步 → 导出 → 推理 → base 与训练后模型的小规模评估 → 缩容到 0 | 各阶段全部走通 |
| E2E-c | 多节点（可选，需另行批准） | 2×p5 EFA 冒烟 | |

实现说明：
- launchpad 代码复制过来时，结构和命名保持不变，只去掉 workspace/AssumeRole 相关部分，但保留 `workspace_id` 列。
- 训练参数预设和日志解析样例都直接取自 `experiments/qwen35_2b_gsm8k` 和 `backends/verl/examples/*`。RayJob 和 EFA 配置参照 awsome-distributed-training 的 verl hyperpod-eks 示例。
- 测试必须 hermetic：boto3 和 k8s client 都只通过工厂获取，测试时注入桩对象。
- 本机 AWS CLI 版本太旧，平台一律使用最新的 boto3，版本写死固定。
