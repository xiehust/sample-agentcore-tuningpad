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

For image-only maintenance, follow [the update contract](./agentcore-image-updates.md):
GET responses contain a rollout-dependent immutable VPC flag, and runtime READY alone
does not prove the DEFAULT endpoint routes to the new version.

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
- **A terminating instance is not capacity.** After an instance starts shutting down, its
  k8s Node stays `Ready` with `nvidia.com/gpu` for minutes. `pools.state` caps the EC2 count
  at the ASG's `InService` instances and skips unschedulable or deleting nodes. Without the
  cap, run-6e153739fb skipped the scale-up and its head pod sat `Pending` on no node.
- **Bad GPU hardware passes every readiness check.** On one p5, `nvidia-fabricmanager`
  failed (`failed to configure all the available GPUs or NVSwitches`; `nvidia-smi -q`
  Fabric State stuck at `In Progress`). The device plugin still advertised 8 GPUs, and every
  attempt died in CUDA init with `Error 802: system not yet initialized`. Before a retry the
  run monitor matches `GPU_NODE_FAULTS` in the failed attempt's log and calls
  `pools.replace_node`: cordon, then an ASG terminate without decrement (EC2) or
  BatchReplaceClusterNodes (HyperPod). It touches only the run's pool, never `system`.
- **Run cost accrues per poll from billed instances** (`pools.billed_nodes`, capped at the
  run's nodes), not from wall-clock × requested nodes. Capacity waits, Spot reclaims and
  pending pods with no instance cost nothing. If the count is unknown, the run is billed
  for its requested nodes, so the budget guard never under-counts.
- **Spot capacity moves by the hour.** The p5 placement score in us-east-2 fell from 9 to 1
  within two hours on 2026-10-08, and the node was reclaimed mid-run. An `UnfulfillableCapacity`
  launch failure with a score of 1–3 means no capacity, not a bad request. Check
  `get-spot-placement-scores` before choosing a region.
- **Scale-down is per pool.** `scale_down_if_idle` counts only active runs on the same
  `instance_group`. When Spot and On-Demand pools of one cluster race for capacity, the
  stopped loser must still drop its own pool to 0, or a late launch bills idle until the
  guardian's idle window ends.
- **The guardian works without the backend** (verified 2026-10-09, us-east-1): with
  `idle_minutes=10` and nothing scheduled, it scaled `ec2-g5-serve` from 2 to 0 on its own
  and recorded `scale_to_zero` / `idle 14 min` in the ledger.

## Logs

- Training logs page by **bytes**. verl's progress bars are full of multi-byte characters
  (`│`), so a full 256 KB page decodes to far fewer characters. `eof` compares bytes read,
  and `read_log` holds back a UTF-8 character split by the page edge. Judging `eof` by
  `len(text)` stopped readers after the first page (run-210c57c0cb) and looked exactly like
  a stalled S3 sync.

## verl LoRA

- **FSDP LoRA must set `rollout.load_format=safetensors`** (`render/train.py`), as the
  toolkit's `fsdp_lora_sync_grpo.sh` does. verl 0.9 defaults to `dummy`. The first weight
  sync then pushes the whole base model as CPU tensors (`collect_lora_params`), and any
  tensor larger than the 512 MB IPC bucket (the 1.27 GB Qwen3.5-4B embedding) is sent
  directly. `rebuild_ipc` then fails with `IndexError: list assignment index out of range`
  (run-a1f2931440). Megatron LoRA uses `lora.merge=True` and does not need this.
- **Megatron must run with `--config-name ppo_megatron_trainer`** (`train_script`), as both
  toolkit Megatron scripts do. verl's default `ppo_trainer` config has no `actor.megatron`
  node, so the first Megatron run failed in Hydra with `Key 'megatron' is not in struct`
  (run-dad915e023). Override-only tests could not catch this: check new override sets by
  composing them offline against the pinned verl wheel's `verl/trainer/config`
  (`hydra.compose`), which reproduces the failure without a GPU.

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

## Eval traces (AgentCore observability)

- **Only evals emit spans.** A training runtime never gets OTEL settings. Its eval-only twin
  `<base[:44]>_obs` (`agents.obs_runtime_name`) has the same image and network, plus
  `obs_environment()`. The template entrypoint (`tp_entry.sh`) starts
  `opentelemetry-instrument` only when `TP_OBSERVABILITY=1`, so the training process is
  unchanged. Verified on 2026-10-09: the `_obs` runtime wrote 283 spans to `aws/spans`, the
  training runtime 0.
- `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` keeps prompt and completion content on the
  spans themselves, so the waterfall reads a single source. Without it, ADOT moves the
  content into a separate logs pipeline.
- `UNIFIED_TRACES_DESTINATION_ENABLED=false` keeps split delivery into `aws/spans`. Unified
  delivery is the default for runtimes created after 2026-07-20, and it needs
  `logs:PutResourcePolicy` on the agent execution role. The query covers both
  (`SPANS_SOURCE`).
- **Transaction Search is account-wide and region-wide.** `enable_transaction_search`
  refuses to run without `confirm`. The console asks first, after a 409 from the eval
  create call. Never call the X-Ray destination update from any other path.
- **The smoke test must pick payloads in the runtime's Region** (`smoke_payloads(region=…)`).
  OfficeBench payloads are S3 URIs, and the runtime role can read only its own Region's
  bucket. Taking the newest dataset from another Region fails the smoke with AccessDenied.
- **Traces are most useful for failed samples.** An ACR failure has no transcript, but its
  spans replay every LLM and tool call up to the crash (ev-916804751a #1: 66 spans). Spans
  stay in `aws/spans` for 30 days, so traces remain viewable after the endpoint is deleted.

## Rollout failures (training)

- The toolkit agent loop classifies failed rollouts:
  - **model:** context limit or `finish_reason=length`. Trained at reward 0.
  - **transient:** invoke or S3 error, or a 500 with no model turns. Retried once on a
    fresh session, then dropped.
  - **agent_error** and **timeout:** dropped.

  In `agentcore_sync` a drop removes only that trajectory, and its siblings still train.
- Chart the trainer's step-line counters (`training/rollout_failure/total_<class>_<action>`,
  `drop_fraction`), not the per-rollout `[rollout-failure]` log lines. Ray's driver log
  dedup (`RAY_DEDUP_LOGS=1`) folds lines that differ only in digits, such as the sid and
  step, so counts taken from those lines come out low.
- `RolloutFailureGuardError` means most rollouts are being dropped by a deterministic bug.
  `stage_monitor` fails the run as `run.rollout_failure_guard` and does not resume from a
  checkpoint. A resume would replay the same bug and burn another
  `agentcore_drop_guard_steps` GPU steps. Only the latest attempt's log counts
  (`ATTEMPT_MARK`).

## Checkpoints

- verl's `max_actor_ckpt_to_keep` deletes `global_step_N/actor` of rotated-out steps and
  keeps the `global_step_N` directory. An index built from `global_step_*` therefore offers
  steps that cannot be exported (the merge fails with "checkpoint ... not found"). The train
  script lists `global_step_*/actor`, and refreshes the index on exit as well.
- The best val step is often not among the last 3 (run-10c5a14110 peaked at step 20). If an
  operator may want to export the best checkpoint, raise `max_actor_ckpt_to_keep`
  (FSx space permitting).
