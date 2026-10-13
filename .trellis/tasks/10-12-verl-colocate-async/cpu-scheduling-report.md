# Single-node Ray / TransferQueue CPU repair (2026-10-13)

Scope: approved minimal internal scheduling repair only. No AWS/Kubernetes/live API calls,
services, cloud state, dependency/lock, GPU/node counts or API/UI fields changed here.
The parent retains the separately approved US$20 cloud test. This is **not GPU acceptance**.

## Requirement-to-check ledger

| Requirement | Check |
| --- | --- |
| Fix p5.4 single-node in sync / colocate_async, including legacy sync | `test_transfer_queue_cpu_budget_preserves_resources`; exact Hydra matrix; local Ray cases |
| Keep p5.48 / multi-node defaults and existing CPU/GPU resources | Same regression matrix, p4d case, exact Hydra matrix |
| Derive bounded internal storage count from Pod CPU; no new knob | Shared `_pod_cpus`, synthetic CPU-capacity regression, unchanged DEFAULT_PARAMS |
| Account for all actor / placement-group reservations | Pinned source audit below; actual TQ bootstrap and synthetic CPU-equivalent actor/PG admission |
| Identify original infeasibility without an endless wait | Original real TQ bootstrap: bounded pending-PG / controller check |
| Preserve safe settings / gates | `test_runs.py`, Ruff, full `make verify` |
| No false real-training claim; isolated cleanup | `GPU=0`, named synthetic resource, constructor-only TaskRunner, shutdown/temp/PID evidence |

## Pinned source evidence and accounting

Inspected private Toolkit source at `/tmp/tp-colocate-qa-1012v1/toolkit`, from
`7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6`. `V` below is its
`.venv/lib/python3.12/site-packages/verl`, `TQ` is sibling `transfer_queue`.
Runtime checks assert verl 0.9.0, TQ 0.1.8 and Ray 2.56.0; no installation or pin changes.

| Component | CPU reservation and source |
| --- | --- |
| Job driver / supervisor | Installed `ray/dashboard/modules/job/job_manager.py:522–523,587`: absent entrypoint CPU request becomes explicit 0 CPU on JobSupervisor. Renderer does not supply an entrypoint CPU request; the shell/Python driver has no separate Ray CPU reservation. The 200m K8s submitter is on the existing system pool, not GPU head capacity. |
| V1 entrypoint | `V/trainer/main_ppo.py:103–166`: `TaskRunnerV1` is plain `@ray.remote`; `run()` unconditionally sets TQ enable before `tq.init`. Applies to **both** trainer modes. Constructor itself has no training side effects. |
| TQ controller | `TQ/controller.py:935`, `TQ/interface.py:176–191`: explicit `num_cpus=1`, created and contacted **before** storage PG. |
| TQ storage | `V/trainer/config/transfer_queue/transfer_queue.yaml:28`: default 8. `TQ/storage/bootstrap/simple_storage_bootstrap.py:35–46`, `TQ/utils/common.py:30–44`: SPREAD PG, one 1-CPU bundle per unit, unbounded upstream `ray.get(pg.ready())`. `TQ/storage/simple_storage.py:137` also explicitly reserves 1 CPU **inside** each bundle, not an extra CPU outside it. |
| Training global pool | `V/trainer/ppo/v1/trainer_base.py:733–787`: one shared actor/rollout/ref pool. `V/single_controller/ray/base.py:146–160,185–215`: manager default `max_colocate_count=3`, PG reserves 3 CPUs + 1 GPU per GPU. |
| WorkerDict / ActorRolloutRef | `V/trainer/ppo/v1/trainer_base.py:239–325`, `V/single_controller/ray/base.py:396–415,628,674–684`: one colocated worker per GPU, requests 1/3 GPU; Ray's specified-resource default holds 1 CPU **within** the existing 3-CPU PG. Ref is not a second external pool. `get_master_addr_port` is a default 1-CPU task scheduled **inside** that PG (`base.py:90,520–528`), not an added steady reservation. |
| Rollout / checkpoint | `V/workers/rollout/replica.py:129–140`: `init_hybrid` reuses the same workers. `trainer_base.py:351–365` fixes naive checkpoint engine; `V/checkpoint_engine/base.py:493–496` directly calls existing actor workers, no additional checkpoint-worker PG. |
| vLLM HTTP server | `V/workers/rollout/vllm_rollout/vllm_async_server.py:1116,1159–1178`: plain remote actor with node affinity; one server for the single-GPU / TP=1 case. No explicit CPU/GPU reservation outside PG. |
| Load balancer | `V/workers/rollout/llm_server.py:600–611`: one plain remote actor. |
| Agent loop | `V/trainer/ppo/v1/agent_loop_tq.py:52,235–246` and `V/experimental/agent_loop/agent_loop.py:1202–1221`: plain remote actor, node affinity; renderer fixes `agent.num_workers=1`. AgentLoopManager and replay buffers are local objects in TaskRunner, not additional Ray actors. |
| Reward loops | `V/experimental/reward_loop/reward_loop.py:308,325–342`, `V/trainer/config/reward/reward.yaml:5,19,29`: 8 plain remote workers, default naive manager, reward model disabled. The explicitly 1-CPU `RewardComputeWorker` in `reward_manager/remote.py:26` is **not** selected by this config. No critic (GRPO) or teacher (distillation disabled). |
| Toolkit failure stats | `src/agentcore_rl_toolkit/backends/verl/rollout_failure.py:143`: explicit `num_cpus=0`. No extra positive CPU reservation. |
| Plain Ray actors | Installed `ray/actor.py:1904–1933`: default actors have 1-CPU **placement/admission** requirement and 0 lifetime reservation; actors with specified resources use 1 lifetime CPU unless explicitly set. Local check records actual assigned resources for TaskRunner and shapes; do not add 1 permanent CPU per default actor. |

The eight dataloader subprocesses (`V/trainer/config/data/legacy_data.yaml:74`),
Python threads, tokenization, gateway, torch/vLLM work and Ray daemons still consume
physical CPU time. They are **not additional Ray logical reservations**. This repair
proves admission, not throughput, physical-core isolation or model memory sufficiency.

### Choice

Current Pod allocation remains `max(4, vcpus - 8)` CPU requests **and** limits,
with no `num-cpus` advertising override. p5.4xlarge stays 16 vCPU / 1 GPU and an
8-CPU Ray head. Original: controller 1 + storage PG 8 = 9 > 8, so bootstrap is
already impossible; adding training PG makes the steady minimum 12.

Only single-node renders compute `min(8, max(1, pod_cpu - 3*gpus - 1 - 2))`.
For the supported small SKU: **8 − 3 training − 1 controller − 2 headroom = 2 units**.
Two spare CPUs are outside all PGs: one to admit default actors / short CPU tasks,
one additional margin rather than filling the node to its admission limit. This also
meets the upstream recommended 2×nodes storage count for the one-node case. Total
reserved is 6, not 8 or 12. Storage total capacity remains 100000; fewer shards do
not reduce that configured capacity. This is an admission safety margin, not a new
physical CPU reservation or a performance promise.

Emit only existing key `transfer_queue.backend.SimpleStorage.num_data_storage_units=2`
(no `+`). No override is emitted when the derived count reaches 8 or nodes > 1;
p5.48, p4d and all multi-node configurations retain upstream defaults. The current
catalog's other training types have ample CPU; no new unsupported SKU policy is implied.

## Changes

- `backend/app/render/train.py`: shared unchanged Pod CPU arithmetic, single-node bounded TQ override.
- `backend/tests/test_runs.py`: resource/default/legacy/mode matrix and synthetic capacity-boundary regressions.
- Task-only `cpu_scheduling.py` and this report. No edits to earlier task helpers/reports or cloud artifacts.

## Verification and cleanup

All commands below completed with exit 0, from repo root unless noted. No test was skipped.
The initial Ruff format check identified two test line wraps; corrected with fileEditor
before the passing focused run. No runtime/CPU-Ray check failed.

```bash
(cd backend && uv run --no-sync ruff check app/render/train.py tests/test_runs.py \
  && uv run --no-sync ruff format --check app/render/train.py tests/test_runs.py \
  && uv run --no-sync pytest tests/test_runs.py -q)
# PASS: 232 tests (20 new cases), focused Ruff check / format

backend/.venv/bin/python .trellis/tasks/10-12-verl-colocate-async/cpu_scheduling.py \
  generate /tmp/tp-cpu-scheduling-1013v1
HF_HUB_OFFLINE=1 RAY_USAGE_STATS_ENABLED=0 \
  /tmp/tp-colocate-qa-1012v1/toolkit/.venv/bin/python \
  .trellis/tasks/10-12-verl-colocate-async/cpu_scheduling.py check /tmp/tp-cpu-scheduling-1013v1
# PASS: 32 exact Hydra compositions, 3 local Ray cases

UV_NO_SYNC=1 make verify > /tmp/tp-cpu-scheduling-1013v1/make-verify.log 2>&1
# PASS: 514 backend tests, Ruff, lifecycle/assets syntax, ESLint, tsc/Vite, i18n (817 keys)
```

`--no-sync` / `UV_NO_SYNC=1` protects the existing prepared environment from dependency
mutation; it does not remove any verification section. The standard gate emits existing
Starlette/Pydantic/fork deprecation warnings. No dependencies installed, no checks weakened.

CPU artifact directory: **`/tmp/tp-cpu-scheduling-1013v1`**:

- `renders.json`: actual renderer output for modes × FSDP/Megatron × full/LoRA ×
  p5.4/p5.48 × 1/2 nodes (32). `compose-results.json`: all composed successfully using
  `ppo_trainer` / `ppo_megatron_trainer`, with 2 units only on small single-node rows,
  otherwise the real upstream 8; storage capacity / actor counts / safe defaults checked.
- `original.log`, `original-result.json`: **PASS_EXPECTED_INFEASIBLE**. Actual TQ
  controller assigned CPU=1; actual 8×CPU=1 storage PG `PENDING` / `NO_RESOURCES`,
  available CPU=7. Three-second bounded `ray.get` times out as expected after the
  PG is visible; no indefinite upstream wait is allowed to escape the test deadline.
- `sync.log`, `sync-result.json`, `colocate_async.log`, `colocate_async-result.json`:
  **PASS_ADMISSION_ONLY** for each. Actual TQ controller CPU=1, two real SimpleStorage
  actors CPU=1 each within their storage PG. Real Ray training-shaped PG
  `{CPU:3, TP_SYNTHETIC_GPU_NOT_HARDWARE:1}` is CREATED; WorkerDict-shaped actor
  holds CPU=1 + synthetic marker=0.3333 inside it. Actual TaskRunner constructor and
  all 11 plain actor shapes (1 agent, 8 reward, 1 server, 1 load balancer) report no
  held CPU. A zero-CPU stats shape also admits. **Available CPU=2** after all persistent
  reservations; one additional explicit 1-CPU probe actor admits successfully.
- `make-verify.log`: complete full gate output, final `verify: PASS`.

Runtime versions recorded: Ray 2.56.0, verl 0.9.0, TQ 0.1.8, torch 2.13.0+cpu,
hydra-core 1.3.3. Read-only cache HEAD/status also confirmed the full Toolkit SHA
above and clean source cache. Helper syntax was parsed with `ast.parse`.

Each case uses `ray.init(address='local', num_cpus=8, num_gpus=0, ...)`, a unique
namespace and private temporary directory. It never connects to an existing cluster,
uses a Ray jobs API, runs `ray stop`, starts a trainer, loads a model, or calls cloud APIs.
TaskRunner is constructor-only; actor bodies other than TQ are explicitly synthetic
resource shapes. Training PG is a real **Ray reservation with a synthetic GPU marker**,
not a real verl model-worker pool or GPU execution. Per-call timeouts plus a 180-second
case deadline / 210-second subprocess timeout bound the checks.

All three JSON reports record `ray_initialized: false`, `surviving_child_pids: []`,
`private_temp_removed: true`. Actors/PGs are removed from the owned cluster and only
its `ray.shutdown()` is called. Evidence files and the pre-existing private CPU environment
are intentionally retained; no owned Ray processes or Ray temp directories are retained.

## Remaining boundary / handoff

The known single-GPU **CPU admission blocker is repaired and validated**. GPU model
initialization, actual rollout/training, optimizer/checkpoint evidence, physical CPU
performance and throughput with fewer TQ shards remain untested here. The parent can
continue the already-approved cloud test under its original stop/budget/cleanup rules;
this subagent did not read or mutate `.run/async-smoke-20261013/state.json`, launch
resources, alter services, or spend the cloud test budget. No spec/source files outside
ownership were edited and no commits made. For the parent's spec-sync step, preserve
this invariant: real Hydra compose alone cannot detect Ray placement-group starvation;
new framework resource defaults must be checked against the rendered Pod CPU limit.
