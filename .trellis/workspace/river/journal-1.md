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


## Session 4: Eval traces AC4 live acceptance; rollout failure policy
<!-- trellis-session: v=2 fp=7b05addeafeb829d -->

**Date**: 2026-10-10
**Task**: Eval traces AC4 live acceptance; rollout failure policy
**Branch**: `main`

### Summary

Closed 10-09-eval-observability with a live AC4 run (ev-916804751a on a g6.2xlarge Qwen3.5-4B endpoint): the eval-only _obs runtime emitted 283 spans and the training runtime 0; real-data browser QA found and fixed overlapping time-axis labels and overflowing exception titles. Started 10-10-rollout-failure-policy: non-model rollout failures no longer train at reward 0. The toolkit half was delegated to the agentcore-rl-toolkit darwin (feat/rollout-failure-policy @ 2788cd3, local only, its verl/gateway/OfficeBench tests re-run here). TuningPad passes the loop policy through from templates, charts the trainer's per-step failure counters, and fails a run without a checkpoint resume when the drop guard trips.

### Main Changes

- Eval endpoint ep-428280d38d deleted and ec2-g6-serve scaled to 0 after AC4; the _obs runtime is kept (idle, usage-billed)
- Per-rollout [rollout-failure] log parsing was built and then removed: Ray driver log dedup undercounts; step-line counters are used instead

### Git Commits

| Hash | Message |
|------|---------|
| `8d24903` | fix(runs): scale down per pool, page logs by bytes, run megatron |
| `e9ee6a3` | feat(evals): per-sample transcripts and AgentCore trace waterfall |
| `a9db316` | docs(spec): record pool, log and megatron lessons; eval traces plan |
| `6c48dc8` | fix(evals): keep long trace axis and exception titles readable |
| `0bfece6` | feat(runs): surface rollout failure policy and stop guard retries |
| `c14d29a` | docs(spec): record eval trace and rollout failure conventions |

### Testing

- [OK] make verify PASS (ruff, pytest, eslint, build, i18n)
- [OK] toolkit: tests/backends/verl 138 passed; tests/examples/test_officebench_rl_app.py + tests/rollout_gateway 111 passed

### Status

[OK] **Completed**

### Next Steps

- AC5 of 10-10-rollout-failure-policy: small live GSM8K run with an injected agent error (needs cost approval)
- Rebuild the OfficeBench agent image so the rl_app.py IndexError fix takes effect
- Decide whether to push/PR toolkit branch feat/rollout-failure-policy; toolkit test_rollout_entrypoint.py reached real S3 (AccessDenied) — pre-existing hermeticity issue
- V4 Megatron GPU validation still pending p5 capacity


## Session 5: Localized job failures and first wrap-up batch

**Date**: 2026-10-10
**Task**: 10-10-project-wrap-up (R1a / R4a / R4b only)
**Branch**: `main`

### Summary

JobPanel now localizes worker error codes during rendering while retaining the original diagnostic. The rollout failure policy task reflects implemented code with live AC5 still outstanding. First-batch checks passed; the wider wrap-up remains open.

### Main Changes

- Reused `localizedMessage` in JobPanel, added en/zh-CN rollout guard copy, kept unknown-code fallback and diagnostic text without duplication.
- Updated the rollout task to `in_progress`, retained its unchecked AC5 and null completion date, and documented toolkit PR #2 merged at `642471f` (contains `2788cd3`).
- Clarification to earlier Next Steps: toolkit push/PR is already done; guardian scale-to-zero without the backend was verified on 2026-10-09. Neither closes rollout AC5 or training auto-resume validation.
- Recorded the successful-job-response localization contract in the error-handling spec. User-approved Playwright CLI/skill installation is user-level only; no project dependency change.

### Testing

- [OK] Frontend lint/typecheck, `make verify`, strict i18n, and independent read-only code review.
- [OK] Playwright CLI with loopback mock API: en/zh-CN at desktop and narrow widths, language switching, unknown/empty codes, diagnostic preservation, deduplication, no-error states and long-text layout.
- [OK] JSON results and screenshots in `.run/job-panel-qa-*`; durable summary in `.trellis/tasks/10-10-project-wrap-up/verification.md`. Only mock GETs, no mutation requests.

### Git Commits

Not committed or pushed, as scoped. Journal/index recorded manually; automatic commit/archive scripts were not run.

### Status and Next Steps

First batch B1–B4 complete; both tracking tasks remain `in_progress`. Remaining work includes live rollout AC5 (cost approval required), OfficeBench image update verification, toolkit test isolation, historical validation gaps, publication and resource-retention decisions. No AWS/K8s operations were performed.

### Follow-up: publication and remaining evidence

The user subsequently authorized commit/push and continuation. `8889a98` was pushed to TuningPad `origin/main`; the branch was verified synchronized. Earlier “not committed” statements describe the pre-publication checkpoint.

Read-only local ledger/job-log inspection found OfficeBench `run-210c57c0cb` completed three training steps, exported step 3 and ran a downstream eval with failed samples; this is partial validation, not the full preset or the new rollout policy. The same job retried and resumed monitoring across backend restarts, but checkpoint-weight restoration is not proven. Details are in `remaining-evidence.md`.

The next offline batch is planned in child task `10-10-toolkit-test-isolation`: mock S3 before app construction and verify background writes without external networking. It remains planning pending the final implementation review; no sibling files or cloud resources were changed.

## Session 6: Isolate toolkit rollout entrypoint tests

**Date**: 2026-10-10
**Task**: 10-10-toolkit-test-isolation (parent R1b)
**Branch**: TuningPad `main`; toolkit `fix/rollout-test-isolation`

### Summary

After user confirmation, fixed only toolkit's `tests/test_rollout_entrypoint.py`: mock S3 before app construction, verify actual background save/completion, and cover handler-error/non-dict results. Production code and dependencies are unchanged.

### Testing

- [OK] Guard self-test blocked real AWS client, external DNS and socket probes before actual calls. The original location-response test failed at real client construction under this guard, as expected.
- [OK] Updated target file: 21 passed; expanded target/client/async-client suite: 75 passed including those 21. Both recorded zero denied attempts. Guard scope is in-process Python/AWS interception, not an OS sandbox.
- [OK] Target Ruff check and format check, independent read-only review, and TuningPad `make verify`.
- [OK] Evidence and commands: `.trellis/tasks/10-10-toolkit-test-isolation/verification.md`; local runner: `.run/toolkit-offline-pytest.py`.

### Git Commits

- toolkit `99ab5d7` — `test(app): isolate rollout entrypoint aws clients`; pushed to `origin/fix/rollout-test-isolation`, not merged into toolkit main.

### Status and Next Steps

Child AC1–AC4 and parent AC2 implementation verification passed. The child remains `in_progress` pending branch merge; no completed task was prematurely archived. Cloud validation, image updates and resource decisions remain open; no AWS/K8s operations were performed.

## Session 7: Authorized image refresh and AWS inventory

**Date**: 2026-10-10
**Task**: 10-10-project-wrap-up (image build/publication only)
**Branch**: `main`

### Summary

The user authorized AWS inventory and builds. Built one OfficeBench ARM image from toolkit `99ab5d7dee`, published it to us-east-1 and us-east-2, and built one FSDP trainer in us-east-1 using the existing CodeBuild project. No runtime deployment, GPU scale-up, training or existing-resource deletion was performed.

### Results and Verification

- [OK] AWS identity matches the project. Both EKS clusters remain ACTIVE; eight EC2 GPU groups have zero desired/actual instances, while two HyperPod CPU system nodes remain. Baseline infrastructure is not fully stopped.
- [OK] OfficeBench tags `ag-98f339686d-99ab5d7dee` / `ag-31d8456654-99ab5d7dee`: same verified ARM manifest, source hashes match, local `/ping` is Healthy, Agent ledger entries ready; temporary probe removed.
- [OK] CodeBuild `tuningpad-trainer-build:348448ba-a480-4330-97b0-07d4a2e7ae35` succeeded. `fsdp-99ab5d7dee` is amd64; uploaded source checksum and build-log push digest match S3/ECR; trainer ledger ready.
- [OK] Bounded operation self-tests, independent read-only operation review, and `make verify`. Full provenance and region-specific digests are in `.trellis/tasks/10-10-project-wrap-up/build-operations.md`.

### Status and Next Steps

Final AWS reads confirm OfficeBench training, observed-eval and standalone runtimes still reference their old images. Image publication is complete, runtime adoption and real rollout AC5 are not. Deployment/model invocation/GPU tests require further approval; actual bill charges were not queried. No Megatron or second-region trainer rebuild was launched.

## Session 8: Adopt OfficeBench images without model invocation

**Date**: 2026-10-10
**Task**: 10-10-project-wrap-up (approved image-only runtime updates)
**Branch**: `main`

### Summary

The user approved updating associated OfficeBench AgentCore runtimes and checking READY only. All four runtime/default endpoints are now version 2 / READY on the verified image. Role, network, platform, environment, lifecycle, metadata and tag fingerprints remained unchanged; no model invocation, GPU scale-up, training, runtime creation/deletion or session stop was performed.

### Operation and Verification

- PUBLIC/V1 standalone updated first. The first VPC request was rejected for echoing the post-rollout read-only `requireServiceS3Endpoint` flag; read-only reconciliation confirmed the old version remained intact. Omitted only that forbidden request field while retaining it in configuration verification, then resumed without re-updating standalone.
- Persisted deploying intent before the mutation to handle ambiguous responses correctly. Offline tests covered drift/rejection handling, stable-token no-replay, Retry-After, DEFAULT routing and preserving historical smoke evidence.
- [OK] Final independent AWS/ledger checks confirmed all four new versions/default routes, expected ECR digest and unchanged fingerprints. Standalone stayed V1/PUBLIC; other runtimes stayed V2/VPC; only the observed-eval twin retained OTEL configuration.
- [OK] `make verify` and read-only reviews; review finding was corrected and tested. Contracts captured in `.trellis/spec/backend/agentcore-image-updates.md`; deployment evidence in `.trellis/tasks/10-10-project-wrap-up/runtime-deployment.md`.

### Status and Next Steps

Parent AC4 (image adoption and safety verification) is complete. Old model smoke results remain historical under `last_smoke.previous`; current validation is `ready_only`, not a new model/quality result. Rollout failure-policy AC5, other real-training validations and resource-retention decisions remain open and need their own approvals.
