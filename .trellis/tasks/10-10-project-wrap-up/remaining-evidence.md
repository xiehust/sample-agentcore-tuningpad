# 剩余事项证据复核 — 2026-10-10

## 检查边界

只读本地代码、Git、Trellis 记录、SQLite ledger 和指定 job 日志；未查询 AWS/K8s。ledger 的状态和更新时间是历史证据，不代表云端实时状态。

## OfficeBench 与自动恢复

**R3b 不能再概括为“完全没有训练实测”。** 本地 ledger 查到以下关联记录：

- `runs.id=run-210c57c0cb`：OfficeBench / Qwen3.5-4B / FSDP LoRA，EC2 p5 Spot；明确设置 `total_training_steps=3`，最终 `status=succeeded`、`progress.step=3`，记录 checkpoint `[1,2,3]`。`run_metrics` 有对应训练步指标，RayJob 最终为 SUCCEEDED。
- `exports.id=ex-93548dfdb3`：该 run 的 step 3 导出成功。
- `inference_endpoints.id=ep-1905453065`：使用该导出，端点已在 ledger 标为 deleted。
- `evals.id=ev-ee3843a85f`：该端点评测流程结束，11 个样本中 3 个打分、8 个失败、0 个截断；已用工具核对分项之和等于总数。流程成功不等于评测样本全部成功，更不是训练收益得到验证。

这些记录对应 2026-10-09，早于本次 rollout failure policy 修复。它们证明了三步训练、导出及评测路径曾运行，不能证明完整两轮训练预设通过，也不能替代新失败策略 AC5。因此不修改模板的“未验证”标签，R3b 改为部分验证。

**R3e 也有部分实测，必须区分恢复类型。** `data/jobs/job-d7184ccc23.log:17-18` 记录失败尝试 a0 后重提 a1；`:19-28` 记录后端重启后恢复 monitor；`:29-34` 最终成功并释放 RayJob、缩到零。结合 ledger 的 `retries=1`，可确认实际发生过重试、后端阶段恢复和最终完成。但日志中的“from the latest checkpoint”是控制面的动作描述，没有 trainer 已加载某个 checkpoint 权重的证据，不能关闭“从已有 checkpoint 恢复训练”的验收。

独立只读复核 tracked docs/Git 未发现上述成功链的完整记录；本轮补充的证据来自 local ledger 和指定 job 日志，不从“没有文档”推断“没有跑过”。

## 仍未闭环的事项

- **OfficeBench agent 镜像**：ledger 中 us-east-1 镜像记录最后更新于 2026-10-09，us-east-2 于 2026-10-08，均早于 2026-10-10 toolkit 修复。尚无本地记录证明修复已部署；仍需核实云端 digest/source 后决定重建，不能仅按 ledger 时间断言线上内容。
- **Trainer 镜像**：ledger 最近 ready 版本仍为 toolkit `0142ae2250`，没有新失败策略 revision 的构建记录。新 run 的 `ensure_image` 会按当前 revision 检查并可能触发 CodeBuild；批准 AC5 时应同时考虑构建费用。
- **Megatron V4**：本地 Megatron run 都是 failed/stopped，未查到成功验收；`run-dad915e023` 是修复前 Hydra 错误，不能把修复提交标题当作实测成功。V4 的确切配置仍需明确。
- **HyperPod 原生 GPU 实例组**：本地有 provider 值的 run 均走 EC2；缺少原生 provider 的成功训练证据。
- **ADOT 冷启动**：现有资料仍无训练 runtime 的前后对照测量。
- **资源善后**：ledger 仍将 `rl-dev-2` 和 `rl-dev-ue-1` 标为 ready，但更新时间陈旧；未访问 AWS，不能确认它们当前存在、容量或计费情况，更没有删除授权。
- **真实验证费用**：本轮没有构建、部署、扩容、评测或删除操作；上述真实操作需明确方案和费用批准。

## 下一批

优先处理不需要云费用的 R1b，已建立子任务 `../10-10-toolkit-test-isolation/prd.md`：仅修复测试文件的 mock 时机和后台保存断言，使用断网验证。子任务保持 planning，等待最终方案确认。

首批代码提交 `8889a98` 已推送至 TuningPad `origin/main`。其余事项继续保持开放，特别是原失败策略 AC5。
