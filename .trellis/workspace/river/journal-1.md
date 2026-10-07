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
