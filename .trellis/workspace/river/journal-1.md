# Journal - river (Part 1)

> AI development session journal
> Started: 2026-10-04

---



## Session 1: Runtime V2, serving on G5, EFA validation, PRD sign-off
<!-- trellis-session: v=2 fp=81d8021742ff0ad5 -->

**Date**: 2026-10-07
**Task**: Runtime V2, serving on G5, EFA validation, PRD sign-off
**Branch**: `main`

### Summary

AgentCore Runtime V2 (platformVersion auto) verified on rl-dev-2; 1x and 2x p5 Spot GRPO runs (2-node NCCL over EFA GDRDMA confirmed); export -> vLLM on g5.2xlarge -> evals via V2 agent; fixed helm pending releases, CPU-node merge (NVIDIA_VISIBLE_DEVICES=void), eval sampling, cluster-delete SG ordering, RayJob release on finish (drain 10 min -> 1.5 min); added node replacement, truncation-aware evals, V2 snapshot static checks; all PRD acceptance criteria ticked with evidence; ops lessons in spec/backend/aws-k8s-operations.md. Open: export-vs-HF eval gap (same weights, 26/50 vs 6/50 truncated).

### Git Commits

| Hash | Message |
|------|---------|
| `b5da679` | Cluster delete: remove VPC AgentCore runtimes and wait for their ENIs |
| `55b8b41` | Serving: G-family inference nodes, CPU-only export, eval sampling |
| `080877b` | Cluster delete SG ordering, RayJob release, multi-node NCCL net logging |
| `fa90138` | PRD: record 2-node EFA validation (NCCL over efa-direct GDRDMA, fast scale-down) |
| `08caeb2` | Node replacement, truncation-aware evals, V2 snapshot checks, ops spec |

### Status

[OK] **Completed**


## Session 2: Training param defaults, export-vs-base A/B, 60-step GSM8K run
<!-- trellis-session: v=2 fp=34a9c920ed6f6216 -->

**Date**: 2026-10-08
**Task**: Training param defaults, export-vs-base A/B, 60-step GSM8K run
**Branch**: `main`

### Summary

Resolved the export-vs-base eval gap by A/B (gap follows trained weights, export is correct); training wizard now shows concrete backend/template defaults; 60-step Qwen3.5-2B GSM8K run on 1x p5 Spot lifted val mean reward from 0.495 (base) to 0.805; checkpoint index fixed to list only steps that still have an actor dir.

### Main Changes

- GET /api/runs/defaults serves DEFAULT_PARAMS and shared AGENT_LOOP_DEFAULTS; Runs wizard shows them as placeholders, Auto kept only for LR, vLLM memory share, backend and tuning strategy; unset steps read 'Run all epochs'
- GSM8K agent returns rewards 0 / stop_reason max_tokens on MaxTokensReachedException; evals count truncated separately
- Checkpoint index counts only global_step_* dirs with actor/, refreshed again on trainer exit (max_actor_ckpt_to_keep pruning broke export of pruned steps)

### Git Commits

| Hash | Message |
|------|---------|
| `29f0221` | spec/PRD: export-vs-base eval gap resolved by A/B (weights, not export artifacts) |
| `a60bdeb` | fix(runs): show concrete training parameter defaults |
| `237dde2` | Checkpoint index lists only steps with an actor dir; record 60-step run (base 0.495 -> 0.805) |

### Testing

- [OK] [OK] make verify; hermetic tests for defaults routing, template loop defaults, explicit/cleared/zero values, checkpoint index
- [OK] [OK] isolated agent-browser checks against a mock API (en/zh-CN, presets, template switching, retry)
- [OK] [OK] real run run-10c5a14110: 60 steps, ~$41 GPU, export step 60 -> vLLM on g5 -> 200-row val eval

### Status

[OK] **Completed**

### Next Steps

- Fill the placeholder backend/frontend specs (00-bootstrap-guidelines)
- Decide whether to keep or delete cluster rl-dev-2 (us-east-2) to stop baseline cost
- Real validation still missing: HyperPod instance-group GPU path, guardian/auto-resume in cluster, Megatron, OfficeBench training


## Session 3: Fill backend/frontend Trellis specs; archive finished tasks
<!-- trellis-session: v=2 fp=313fdf387e6620e6 -->

**Date**: 2026-10-08
**Task**: Fill backend/frontend Trellis specs; archive finished tasks
**Branch**: `main`

### Summary

Archived 10-07-training-param-defaults (code in a60bdeb) and back-filled the 10-07 journal entry; replaced all trellis-init placeholder specs (5 backend + 6 frontend + 2 indexes) with source-backed guidelines, independently reviewed against the code; bootstrap task archived.

### Main Changes

- backend specs: directory structure, database (SQLAlchemy/SQLite, JSON columns, nullable-column upgrades), error handling (AppError envelope, worker vs HTTP paths, apiErrors localization), logging, quality gate
- frontend specs: directory structure, v2 kit components, useLoad/useJob hooks, state ownership (display defaults vs explicit overrides), type safety, quality/i18n

### Git Commits

| Hash | Message |
|------|---------|
| `daa2169` | docs(spec): fill backend and frontend Trellis guidelines from source |

### Testing

- [OK] [OK] make verify PASS; placeholder grep empty; index tables match files; 29 relative links resolve; 13 cited symbols spot-checked by grep

### Status

[OK] **Completed**

### Next Steps

- Decide whether to keep or delete cluster rl-dev-2 (us-east-2)
- Real validation still missing (needs cost approval): HyperPod instance-group GPU path, guardian/auto-resume in cluster, Megatron, OfficeBench training
