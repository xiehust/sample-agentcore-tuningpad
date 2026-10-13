# 同步 / 同池异步：已批准的最小设计

用户已在最新方案后回复“ok”。允许按本文实现和本地验证，不授权 GPU 构建/部署/训练；云测试另行确认配置与预算。

依据：[research.md](research.md)；Toolkit `7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6` + verl 0.9.0 + TransferQueue 0.1.8。不跟随 main，不改依赖、训练架构或既有 profile/LoRA 支持策略。

## 1. 单一模式合同

`RunWizard → CreateRun.training_mode → preview.training_mode / Run.spec.training_mode → services.runs.render → trainer registry`。

- `render.train` 是纯模式类型/alias 映射及批次/配对校验的唯一 owner，`services.runs` 组合该校验和 compute deadline 校验；router 引用同一 `TrainingMode = Literal["sync", "colocate_async"]`，无 render→service 反向依赖。mode 不放 `params` 或资源 Plan，不新增 schema/迁移。新 Run 显式存规范化值。
- 顶层请求省略为 sync；null、空串、错误类型、未知值及 separate_async 走既有 422 `request.invalid`。旧 spec **只有缺键**按 sync，不回填；显式非法持久值以稳定 domain error 拒绝，不静默降级。
- params/plan_overrides 不增 mode 或 Hydra 通道，维持 unknown-param/bad-plan-override 错误。preview/create 共用校验；最终 plan 覆盖后检查 profile/strategy 合法且配对，不仅检查 prefer/tuning。
- 在 `services/runs.py` 集中复用有效模式/批次/相关 plan 合同校验；API 在写 Run/Job 前、preflight 在镜像/扩容前、render 在每次提交前执行。错误需双语 code；不重构全部 params/plan 校验。
- retry、后台重启、手动 resume 均读取原 spec，不能换 mode；保留现有 `resume_mode=auto`，但它不代表完整恢复。

## 2. 固定渲染与 SDK 合同

| 控制台值 | `trainer.v1.trainer_mode` |
| --- | --- |
| sync（默认） | agentcore_sync |
| colocate_async | agentcore_colocate_async |

保留 external module、V1、PayloadDataset、GRPO、actor-only、非 distillation、`seq-mean-token-sum`、rollout.mode=async、rollout logprobs 与现有 IS 配置。异步额外显式固定：

```text
trainer.v1.colocate_async.num_warmup_batches=1
trainer.v1.sampler.max_off_policy_threshold=8
trainer.v1.sampler.max_off_policy_strategy=drop
+trainer.v1.colocate_async.parameter_sync_step=1
```

最后一键在原 YAML 不存在，须用 `+`，不能放到 alias 子树。sync 保持已有有效输出（底层 parameter_sync_step 缺省为 1）。两模式均校验 B、m 为正整数且 B%m=0；mini 省略/null 继承 B，0/负数/非整数/布尔/数字字符串拒绝。B/m 是每次 actor 更新的分区数，不是固定轨迹行数；保留 SDK 扩行/padding 机制。

FSDP/Megatron × full/LoRA 均保留现有渲染分支与自动选型；FSDP LoRA safetensors、Megatron LoRA merge、Megatron 专用 config-name 不变。两模式×四策略做离线合同检查，不宣称八种硬件组合已验证。保持同一节点池和资源数量，不新增采样节点或调度服务。

## 3. 最小 UI 与风险说明

- 模型步骤增加 V2 双选项（默认同步）及双语帮助；独立 typed state，preview/create 同一 body，预设/模板切换和失败重试不重置模式。
- review、费用确认、详情配置显示模式；详情缺键显示同步，非法值显示异常而非伪装同步。PlanCard 不变。
- colocate_async 标明实验性：同池分时而非 GPU 内核同时训练/推理；不保证更快；失败整组丢弃补采，**没有 sync 的 drop-fraction guard**；重启不保证在途样本完整恢复。选择时及详情可见，review/费用确认保留风险提示，不另建风险确认平台。
- **异步必须有有效时限，不只提示**：复用 `compute.max_hours`，仅当 mode=colocate_async 时要求有限正数且 `int(max_hours * 3600) >= 1`；须符合现有 Compute 上限。API 预览/创建、preflight、render 共用校验，分别在写入前、镜像/扩容前、提交前拒绝不满足者；错误两 locale 配对。沿用现有 `activeDeadlineSeconds` 渲染，最终 manifest 断言正整数存在。
- UI 不新增时限字段或开关，复用算力/预算步骤的最大时长（当前默认6小时）；用户清空后必须补回合法值才允许异步提交，不悄悄改数值。说明这是每次 RayJob attempt 的时限，既有 max_retries 可延长总体等待/用量，预算仍是估算而非账单硬封顶。
- 同步、旧 spec 缺模式的可空时长行为不变；其余预算、guardian、费用确认、认证/AgentCore SG/registered sessions/IRSA 保持。不能把 guardian 描述为持续补采检测器；不新增 watchdog 平台或移植 sync-only async guard。

## 4. 运维、兼容与发布边界

优先复用同 SHA/对应 profile 镜像；仅在获批操作时核 digest、实际版本/类，不以 ready 标签代替证明。固定 TQ 0.1.8 无队列 checkpoint，现有 Megatron model-only 无优化器连续性保证，均如实显示/验收；不升级依赖修补。

主体实施范围为现有 runs router/service/render/pipeline、API DTO、Runs 页面、两份 locale 与测试。验收中另外定位了 AC4 非有限数值错误响应的根因（core/errors.py JSON 编码）和 AC3 窄屏导航裁切（v2.css 两个导航选择器），只做局部编码/换行修复并补回归，不改错误 envelope 或整体 UI 设计。planner、ORM、镜像构建策略、metrics 平台不改。已有 JSON 字段无迁移；要撤回时先停止新建 async，再按获批范围停止/收尾 async Run 后回滚，不能让旧 renderer 把已存 async Run 当 sync 重试；不改历史 spec。代码完成门禁与另行授权的真实训练门禁分开记录，未云验不得宣称生产验证完成。
