# AWS / Kubernetes Operations

> Lessons from real runs (2026-10-04/05, us-east-1 / us-east-2). Each rule names the failure
> it prevents. Apply them in `pipelines/` and `services/`.

## Pipeline stages are interruptible

`uvicorn --reload` restarts the backend whenever a file changes. A stage can die at any
point and run again from the start. Every external tool call has to recover from a
half-finished previous attempt.

- **Helm**: a killed `helm upgrade --install --wait` leaves the release in `pending-install`
  or `pending-upgrade`. Every later install then fails with `release: already exists`.
  `kube.helm()` fixes this itself before an install: it uninstalls a pending first install
  and rolls back a pending upgrade (`_clear_stuck_release`). Keep calling helm through
  `kube.helm`.
- Check real state before creating anything, for example `find_runtime`,
  `ng.describe`, `kube.get`.

## AgentCore Runtime

- **Platform version.** Always pass `platformVersion` on both create and update. If update
  leaves it out, the runtime keeps its current version, so a V1 runtime would never move to
  V2. The default is `auto`: V2 in `V2_REGIONS`, V1 everywhere else. The create response does
  not return the version; read it with `get_agent_runtime` once the runtime is READY.
- **V2 timing.** Create and update take several minutes (we measured about 12.5 min, versus
  seconds on V1). Use `READY_TIMEOUT_S["V2"]`. Calling update while the runtime is
  `CREATING` or `UPDATING` returns `ConflictException`, so wait until it leaves the
  in-progress state first.
- **V2 snapshot safety.** V2 snapshots the process once startup finishes, so import-time
  values are shared by every session. `static_checks` warns when code calls
  `uuid`/`random`/`secrets`/`os.urandom`/`time`/`datetime.now`/`getpid` at module level.
  Generate these values inside the rollout handler.
- **Truncated agent turns.** When a turn hits `max_tokens`, Strands raises
  `MaxTokensReachedException`, which reaches the client as a 500. Agents should catch it and
  return `{"rewards": 0.0, "stop_reason": "max_tokens"}`. `summarize_eval` counts
  `truncated` separately from infrastructure failures.
- **VPC ENIs.** VPC-mode runtimes hold hidden ENIs on `sg-acr`. These stay for some time
  (more than 15 min) after the runtime is deleted.

## Deleting a cluster

The order is fixed:

1. Workloads and node groups.
2. AgentCore runtimes. Wait for their ENIs to go away.
3. Platform SGs (`sg-acr`, `sg-nlb`). Retry on `DependencyViolation` for up to
   `SG_RELEASE_TIMEOUT_S`.
4. The CloudFormation stack.

The SGs sit in the stack's VPC but are not stack resources. If any of them remains, the
stack ends in `DELETE_FAILED` on `VPC`. After a re-issued delete, report
`stack_failure_reason`, which reads only the events of the latest operation. Older events
are stale.

## GPU node lifecycle (cost)

- **Release the RayJob as soon as the run ends**, in `stage_finalize` and on failure.
  KubeRay keeps the RayCluster for `ttlSecondsAfterFinished` (600 s) and re-creates evicted
  workers. The node group drain then sits in `Terminating:Wait` and the GPUs bill for about
  10 more minutes. Measured: about 10 min before the fix, about 1.5 min after.
- **EFA node groups** are single-AZ with a cluster placement group. Choose the AZ with the
  best Spot placement score (`az_id`). `pick_subnets` picks by free IPs, which says nothing
  about capacity.
- **Proving EFA is in use.** Multi-node pods run with `NCCL_DEBUG=INFO` and
  `NCCL_DEBUG_SUBSYS=INIT,NET`. The train log must show
  `NET/OFI Selected provider is efa` and `via NET/Libfabric/<n>/GDRDMA`. Containers cannot
  read the EFA hw_counters, so they are no use as evidence.

## Containers on CPU nodes

- The trainer image is a CUDA image with `NVIDIA_VISIBLE_DEVICES=all`. On the GPU-less
  `system` node, the NVIDIA runtime then fails with `failed to initialize NVML: Driver Not
  Loaded`, before the pod writes any log. CPU-only Jobs built from GPU images, such as the
  merge Job, must set `NVIDIA_VISIBLE_DEVICES=void`.
- Size `/dev/shm` (a memory `emptyDir`) from node RAM. A g5.2xlarge has only 32 GiB.

## Instance catalog

- `instances.spec(t)` accepts P-family training types only. Pass `serving=True` to accept
  the inference-only G-family types (g5/g6/g6e) on EC2 node groups and vLLM. Never hand a
  serving type to a HyperPod instance group, the planner, or a run.

## Evaluation

- Evals must use the same per-turn sampling as the run's validation:
  `max_tokens = max_tokens_per_turn` and `temperature = val_temperature`
  (`serving.eval_sampling`). Without these limits a turn can run up to vLLM's
  `max_model_len`. In our case 62% of evals failed and they ran slowly.
- **A 2-step smoke run is not a quality signal, but it does change the policy.**
  Two GRPO steps at lr 5e-6 changed 14.3% of all bf16 weight elements, each by about one
  bf16 ulp (median relative change 6e-3), in 243 of 581 bf16 tensors. Adam's first steps
  move every parameter by about lr·sign(g). The fp32 master weights cross a bf16 rounding
  boundary for roughly one element in seven, and that coherent nudge is enough to shift
  behaviour: the step-2 export truncated 32–35/50 GSM8K evals, against 3–6/50 for base. Do
  not judge a model by tensor-level statistics (the median tensor diff is 0); count changed
  elements instead.
- **Export is faithful (A/B, 2026-10-07, 50 val, identical sampling):**

  | Variant | Truncated |
  |---|---|
  | HF base (two runs) | 4/50 and 6/50 |
  | base weights, MTP dropped, re-saved | 3/50 |
  | export weights + base config/tokenizer/templates | 35/50 |
  | export without `generation_config.json` | 34/50 |
  | export with the 36 fp32 tensors restored | 32/50 |
  | export as produced | 26/50 |

  The gap follows the weights only. None of these is a cause: MTP removal, the fp32→bf16
  cast, the rewritten tokenizer, `generation_config.json`.

## Checkpoints

- verl's `max_actor_ckpt_to_keep` deletes `global_step_N/actor` of rotated-out steps and
  keeps the `global_step_N` directory. An index built from `global_step_*` therefore offers
  steps that cannot be exported (the merge fails with "checkpoint ... not found"). The train
  script lists `global_step_*/actor`, and refreshes the index on exit as well.
- The best val step is often not among the last 3 (run-10c5a14110 peaked at step 20). If an
  operator may want to export the best checkpoint, raise `max_actor_ckpt_to_keep`
  (FSx space permitting).
