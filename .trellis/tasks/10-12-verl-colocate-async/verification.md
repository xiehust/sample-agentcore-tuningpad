# 2026-10-13 提交前复核（代码已提交，任务保持 in_progress）

toolkit-git-source 已单独提交为 634b80c 后，当前工作树 = 该 HEAD + 本任务全部改动。以 `UV_NO_SYNC=1 UV_OFFLINE=1 HF_HUB_OFFLINE=1 AWS_EC2_METADATA_DISABLED=true`、清空 AWS 凭据运行 `scripts/verify.sh`：exit 0，ruff/pytest（514 passed）/lifecycle/bash -n/eslint/build/i18n 全部 OK，`verify: PASS`。按用户选择提交本任务代码与任务工件；`.gitignore` 的 `.playwright-cli/` 及 `.claude/skills/playwright-cli/`、`.playwright/` 为 playwright-cli 安装产物，未纳入。AC5 真实 GPU 训练仍为容量 BLOCKED，任务保持 in_progress 以待新的云测批准；本次无任何云操作。

# 2026-10-13 最终补充：CPU 调度已修，真实训练容量受阻

用户批准最小CPU修正及独立US$20测试后，单卡队列预留已改为2单元并保留原Pod8CPU；真实隔离Ray调度（无GPU，合成加速器）、32组Hydra配置、514项后端测试通过。父代理最终make verify为 bg-3c764401-f0ec-48fd-9b38-aa765908c346，05:42:58Z exit0；详见cpu-scheduling-report.md。

真实请求run-dec23e4174/job-05313353ae停在capacity，15分钟截止后取消：ASG12条失败活动均未分配实例，未提交RayJob、未执行训练更新或生成checkpoint，AC5仍BLOCKED。临时节点组/LT/ASG/Runtime已核查删除、无实例/活动Job/本Run K8s对象；不将训练台账0当成总费用0。详见cloud-test-report.md与`.run/async-smoke-20261013/state.json`。以下为历史本地验收记录，早期“尚未获云授权”状态已被本节取代。


# 2026-10-12 Review corrections (latest local evidence)

This section supersedes the earlier narrow-layout exception below. The correction worker
read the real handler/CSS and reproduced both findings before editing. Only
`backend/app/core/errors.py`, the overflow regression in `backend/tests/test_runs.py`,
`frontend/src/v2/v2.css`, minimal backend spec notes and this task's artifacts were changed.
All prior Git-source work was preserved; no dependency/lock/source-cache/engine/ORM,
real config, old task or HTML documentation edits. No installs, environment sync,
real :8100 writes, AWS/K8s, service restart, CodeBuild or GPU operations occurred.

## Findings and minimal fixes

- Raw JSON `1e999` / `-1e999` parses to a non-finite float. The validation handler passed
  that input through to JSONResponse, which raised `ValueError` instead of returning 422.
  Reproduction: `pytest tests/test_runs.py -q -x -k overflow_deadline` failed before the
  fix; `/tmp/tp-colocate-fix-overflow-before.log` retains the stack. The handler now
  stringifies only non-finite floats while retaining finite numbers and the existing
  exception encoder. Four new cases cover both modes and both signed literals against
  preview/create, exact 422 `request.invalid` detail, unchanged Run/Job counts and no
  capacity/K8s effects. This is an error-serialization fix, not a deadline bypass fix.
- Pre-fix English wizard at 390px reproduced scrollWidth=562. The existing narrow-screen
  media query now gives only `.v2-steps, .v2-subtabs` `max-width:100%; flex-wrap:wrap`.
  No document overflow hiding, UI framework, parameter change or broad restyling.

## Correction verification results

Parent reconciliation after the correction worker was terminated: all correction test tasks were already terminal. JUnit parsing confirmed **494 backend tests, 138 pinned SDK CPU tests, zero failures/errors/skips**. The corrected `compose-results.json` contains **8 PASS** combinations and the actual TQ checkpoint gate remains False. Browser `layout-checks.json` contains **16 passing geometry/retry groups**; parent opened the Chinese narrow model page and English confirmation screenshot with imageViewer. These are mock fixtures; long real-world preset labels were not exhaustively covered.

The parent then ran complete `make verify` with the existing backend environment, `UV_NO_SYNC=1 UV_OFFLINE=1 HF_HUB_OFFLINE=1 AWS_EC2_METADATA_DISABLED=true`. Task `bg-29ede98c-55e0-4048-86e1-8712d862c4f7` exited 0 and ended `verify: PASS`; every Ruff/pytest/syntax/ESLint/TypeScript/Vite/i18n section passed. This gate includes the corrected non-finite error encoding and narrow navigation CSS. Dependency/fork deprecation warnings remain; no dependency was upgraded or lockfile changed.

## Reproduction (existing environments only)

Correction artifacts: `/tmp/tp-colocate-fix-1012v2/{backend.log,backend.xml,sdk.log,sdk.xml,compose.log,compose-results.json,layout.log,layout-checks.json}`. Exact repeat commands are recorded in the settled task commands: `bg-9187e57c-7b8e-48bd-bfa8-2868fb14cd26` (compose), `bg-861bc551-51f7-4ec5-babd-df1d0b597695` (SDK), `bg-2d87855d-cdce-41d6-bc6c-3f35e28832cc` (browser), `bg-c729155f-bde3-40a1-a5b8-cd07b9599868` (backend/Ruff); all exit 0. Reuse `/tmp/tp-colocate-qa-1012v1/toolkit/.venv` for pinned CPU checks without installing or modifying shared source.

The final parent gate was run from the repository root with `UV_PROJECT_ENVIRONMENT=<repo>/backend/.venv` plus the offline/no-sync environment above. Product changes were complete before that gate; subsequent task-record updates do not change tested code.

## Acceptance status and cleanup

AC1–AC4 local/CPU/mock evidence is complete within the test coverage described here; AC5/AC6 remain unauthorized and NOT RUN. This is not cloud/GPU/throughput certification.

The parent stopped only owned service task `bg-02b73b87-8f79-46a5-bab1-32e877605c78`; its supervisor exited 0 and confirmed Chrome 1075018 (0), Vite 1075017 (143), mock 1075016 (-15) reaped. `ss` confirmed no listeners on mock 18187/Vite 5187. Existing real backend/frontend were not restarted or used for writes by these checks.

Dependency hashes remain pyproject `819ae313a0090faf613039b5d352f1faad12c94d3bf9b5c64ef1c8ce13869cf7`, lock `72f62d046f2f27ef2a1f7beb8fe428f834c19600ded7a3ed54fb5db8a86965c5`. No commit/archive performed.

After local acceptance, the parent separately made read-only Pricing/ECR queries to prepare `cloud-test-plan.md`: p5.4xlarge us-east-2 quote 6.88 USD/node-hour; the existing fixed-SHA FSDP image still exists with the expected digest. Those planning queries are not GPU-test execution or resource-creation authorization; no new Runtime/nodegroup/build/training was started.

---


# 2026-10-12 Local acceptance update (authoritative)

**Fixed CPU contracts PASS; hermetic full gate PASS; mock-browser functional checks PASS with known narrow-layout exception. GPU/runtime/training/recovery/cleanup acceptance NOT RUN (not authorized).** This supersedes the historical pending/formatting handoff retained at the bottom; it is not production/GPU certification.

This verification worker changed only this task's test helpers and report using fileEditor. No product, dependency/lock, shared Toolkit source, real config, other task or cloud resource was changed. Existing unrelated dirty files were preserved. No delegation, real :8100 submission, `start.py`, AWS/K8s operation, CodeBuild, cloud image build or GPU training occurred. Private dependency preparation used public fixed-lock downloads; test runs used offline/no-sync flags.

| Gate | Result / evidence |
| --- | --- |
| Backend API/storage/render/preflight/retry/restart regressions | PASS in the complete hermetic gate: **490 pass dots, no failures/skips**. Includes `test_mode_api_persistence_round_trip`, invalid requests/no-side-effects, dictionary injection, batch strictness, deadline boundaries, legacy read-only behavior, retry/manual-resume and backend-restart cases in `backend/tests/test_runs.py`. Earlier parent targeted runs/engine pass is inherited evidence, not repeated as a separate result here. |
| Full `make verify` | PASS, exit 0, task `bg-08cf31bb-405b-4174-a789-eeee348d245a`; Ruff, 76 formatted files, all pytest, lifecycle/shell syntax, ESLint, TypeScript+Vite and 817-key locale parity. `/tmp/tp-colocate-verify-1012v1.log`. |
| Strict i18n / standalone frontend lint/typecheck | Parent supplied PASS before this worker; unchanged frontend. Full gate independently rechecked lint/build/parity, not strict i18n. |
| Actual fixed-verl Hydra compose | **8/8 PASS**, 0 skips: sync/colocate_async × FSDP/Megatron × full/LoRA; real generated service manifests and trainer constructors, not string-only assertions. |
| Actual fixed Toolkit CPU verl tests | **138 passed, 0 skipped, 0 failed, 0 errors**, 44.33s; JUnit retained. Includes 27 trainer registry/batching, 6 worker batching, 26 rollout-failure tests. |
| Browser | Real raw Chrome + agent-browser, mock-only. en/zh-CN, keyboard, 390px, payloads, save/refresh, legacy/invalid/null, preset/template switch, preview/create failure retry, metadata retry, invalid max_hours and cancel-without-create checked. Global narrow overflow remains (below). |
| Final helper syntax / diff whitespace | `ast.parse` all seven task Python helpers: PASS. `git diff --check`: PASS. |
| GPU/cloud | **NOT RUN / separately authorized gate remains open**. No claims about real optimizer updates, checkpoints, runtime pause/resume, throughput, queue recovery or billable-instance cleanup. |

Full gate command (no backend environment synchronization):

```bash
UV_NO_SYNC=1 UV_OFFLINE=1 HF_HUB_OFFLINE=1 AWS_EC2_METADATA_DISABLED=true make verify \
  > /tmp/tp-colocate-verify-1012v1.log 2>&1
```

Warnings, not skips: Starlette/httpx and Pydantic deprecations, multiprocessing fork warnings; CPU SDK additionally warns about optional GPU engines unavailable. No required CPU test dependency was absent. GPU engines were not installed/certified by this CPU check.

## Fixed CPU environment and commands

Identity was checked before preparation:

- Toolkit source HEAD **7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6**, clean; copied using `git archive <SHA>`, not a sibling or mutable shared working copy.
- Existing `/tmp/verlsrc/verl-0.9.0-py3-none-any.whl` SHA256 **6e9374db1e2ab346a0aafe9a202bee1b239a042def2f2ccd6db2a4cd04c6308b** verified.
- Toolkit source and private-copy `uv.lock` both SHA256 `b18afad35c732140d03818bc134967992457cf00ff06da8c47f2c6b77c6d951c`; source remained clean after testing.
- Private environment: `/tmp/tp-colocate-qa-1012v1/toolkit/.venv`; private uv cache `/tmp/tp-colocate-qa-1012v1/uv-cache`. No installation into `backend/.venv` or shared Toolkit cache.
- Python **3.12.13**, uv **0.12.23**; Toolkit distribution **0.1.5** at the above source SHA, verl **0.9.0**, TransferQueue **0.1.8**, torch **2.13.0+cpu**, torchvision **0.28.0+cpu**, transformers **5.12.1**, Hydra **1.3.3**, pytest **9.0.2** — selected by the fixed Toolkit lock and existing `verl-ci` + `dev` groups, not by changing dependencies.

Actual preparation and offline execution (paths relative to repository root unless `cd` shown):

```bash
TASK=.trellis/tasks/10-12-verl-colocate-async
WORK=/tmp/tp-colocate-qa-1012v1
python3 "$TASK/prepare_cpu.py" "$WORK"
# prepare_cpu performs uv sync --frozen --group verl-ci --extra dev,
# with the private UV_CACHE_DIR and existing Python interpreter.

PYTHONDONTWRITEBYTECODE=1 AWS_EC2_METADATA_DISABLED=true \
  backend/.venv/bin/python "$TASK/framework_contract.py" generate "$WORK"

(cd "$WORK/toolkit" && \
 UV_CACHE_DIR="$WORK/uv-cache" UV_OFFLINE=1 HF_HUB_OFFLINE=1 \
 HF_HUB_DISABLE_TELEMETRY=1 AWS_EC2_METADATA_DISABLED=true PYTHONDONTWRITEBYTECODE=1 \
 uv run --offline --no-sync pytest tests/backends/verl -q -ra \
 --junitxml="$WORK/sdk.xml")

UV_OFFLINE=1 HF_HUB_OFFLINE=1 AWS_EC2_METADATA_DISABLED=true PYTHONDONTWRITEBYTECODE=1 \
 "$WORK/toolkit/.venv/bin/python" "$TASK/framework_contract.py" check "$WORK"
```

To reproduce preparation choose an unused `WORK` path (the helper refuses overwrite); the existing environment can rerun checks offline without preparation. No source or lock regeneration is needed. The original wheel is re-hashed by preparation; `uv sync --frozen` installs the fixed lock artifact. For long commands use managed `start`/`wait`, not a blocking foreground wait.

`framework_contract.py generate` calls actual planner + `services.runs.render` for eight manifests with B=8/m=4 and a **1-second** deadline. `check` calls actual Hydra `compose` with unmodified generated overrides for `ppo_trainer` and `ppo_megatron_trainer`. It asserts native config subtrees, warmup=1/staleness=8/drop/sync-step=1, GRPO/actor-only/non-distillation/loss/IS/logprobs, resource counts, FSDP dynamic batching, Megatron microbatch=1 and config-name, FSDP LoRA safetensors and Megatron LoRA merge. Four negative controls remove the required `+` from the async native key and require real `ConfigCompositionException`.

All eight configs instantiated the actual registered trainer classes on CPU: alias→native normalization, sync step=1, two actor partitions, DP×partition alignment; sync alone inherits `RolloutFailureGuardMixin`. Actual verl `_tq_supports_checkpoint()` returned **False** under installed TQ 0.1.8. No model/actor/GPU training was initialized.

Artifacts in `WORK`: `renders.json`, `compose-results.json`, `compose.log`, `sdk.log`, `sdk.xml`, `evidence-summary.json`. Preparation log: `/tmp/tp-colocate-prepare-1012v1.log`.

Background terminal results: preparation `bg-7a4a38cb-5822-4826-b47a-161184385b77` exit 0; SDK `bg-55c859a8-81f7-431c-9985-1a14f6494fd0` exit 0; compose `bg-493a6681-7cfe-4c1e-94e4-9d108b6ae2bb` exit 0.

## Browser environment, assertions and artifacts

Loaded `agent-browser` skill and `agent-browser skills get core` first. Used installed **agent-browser 0.27.0**, verified executable `/snap/chromium/current/usr/lib/chromium-browser/chrome` (**Chromium 155.0.8059.39**). Own fresh HOME/profile: `/home/ubuntu/snap/chromium/common/tp-colocate-qa-1012v1/{profile,...}`. Raw Chrome ran headless with debug-port **0**, no `--no-sandbox`, elevation, global attach, existing profile or other session. Its actual CDP endpoint was `127.0.0.1:42065` (verified listener). Wrapper clears inherited `AGENT_BROWSER_*`, uses empty config and independent session `tp-colocate-qa-1012v1`, and attaches only that profile's CDP port.

Own services: mock **127.0.0.1:18187**, Vite **127.0.0.1:5187**, `TUNINGPAD_API=http://127.0.0.1:18187`. Nothing used real :8100. `mock_api.py` checks required IDs/model, top-level enum, params separation, async deadline, node/scale-down settings and explicit cost confirmation; create must equal the **last successful preview payload**, except `confirm_cost`. It records every request including failures and rejects invalid payloads. Fixture statuses are simulated, not evidence of real training.

Executed workflow and assertions:

- English new wizard default sync; keyboard runtime/dataset selection. Sync accepts cleared max_hours and sends `null`; actual create saves sync and config remains sync after reload.
- English async selected by Tab/Space; risk text explains time sharing, no speed guarantee, no sync drop-fraction guard, resampling, TQ 0.1.8 recovery and per-attempt deadline limits.
- Apply preset, switch GSM8K→OfficeBench template/runtime, then injected preview HTTP 503 and retry: mode stays async, runtime becomes `rt-office`, params stay `{train_batch_size:8,rollout_n:4}`. Actual failed/successful payloads are identical.
- Clear async max_hours, then enter `0.00001` using actual keys: error/aria-invalid and disabled Next. No invalid preview/create was sent. Restore `0.01`, inspect review + fee modal; cancel sends no create. Inject create 503, explicitly reconfirm, retry exact same async payload; saved mode survives reload.
- Both languages: legacy missing key renders sync without changing the fixture; explicit corrupt mode **and null** render invalid-mode alert/config, not sync, before and after reload.
- Chinese new default sync; held metadata failure blocks Next, choose async with keyboard, release failure and click localized Retry; mode survives retry and preset. At **390×844**, clear/tiny deadline refusal, review/fee mode/risk wording, cancellation, successful exact-payload create and reload checked.
- Additional English **390×844** model/confirmation/detail screenshots; mode keyboard-reachable, warning readable, modal cancellable. Existing overall overflow is an explicit exception below, not called responsive PASS.
- Recorded totals: **9 successful previews; 3 successful creates** (`mock-1` sync/null hours, `mock-2` async/OfficeBench, `mock-3` async/GSM8K); one expected failed preview and one expected failed create. **Exactly 4 create POSTs**, all explicit confirmations (3 success + 1 injected failure), so the three Cancel actions contributed no create POST. Mock violations `[]`; browser `errors` output empty.

Reproduction helpers (new unused WORK basename for a fresh browser HOME; start services as a managed background task):

```bash
# After prepare_cpu/framework_contract generate from the previous section:
python3 "$TASK/browser_services.py" "$WORK"
# In another command; browser_cli reads own DevToolsActivePort:
python3 "$TASK/browser_cli.py" "$WORK" open 'http://127.0.0.1:5187/runs?view=new'
python3 "$TASK/browser_cli.py" "$WORK" snapshot -i
python3 "$TASK/browser_acceptance.py" "$WORK"
# Original-session artifact checks (expects its 3 creates, including manual English flow):
python3 "$TASK/summarize_artifacts.py" /tmp/tp-colocate-qa-1012v1
```

`browser_acceptance.py` covers bilingual legacy/invalid/null plus Chinese new/metadata/keyboard/narrow/deadline/cancel/create/reload. English interactive steps are reproducible from `browser-transcript.jsonl` using snapshot-ref refreshes; actual commands, returned text, errors and screenshot paths are retained there. Raw keyboard controls are important: use focus→Control+a→Backspace→keyboard type→Tab for number inputs, rather than treating a changed DOM value as proof of React state.

Original execution: `browser_acceptance.py WORK` passed six detail/reload cases, then the harness's one-shot metadata failure assertion failed due to StrictMode's second request. Fixed **only the test helper** to hold failure until Retry; resumed only wizard via `--wizard-only` (exit 0), preserving the six passing cases. The helper subsequently gained an explicit initial loopback `open` and language selection so it can start from fresh about:blank; these duplicate the manually executed entry operations, not a new product check.

Artifacts under `/tmp/tp-colocate-qa-1012v1/`: `requests.jsonl`, `browser-transcript.jsonl`, `browser-checks.json` (13 automated passing groups), `browser-acceptance.log`, `browser-acceptance-wizard.log`, `browser-services.json`, `mock.log`, `vite.log`, `chrome.log`, `evidence-summary.json`. Screenshot paths below are the **actual CLI-returned paths**, not guessed directories; representative images were opened with imageViewer and visually inspected:

- `en-async-model.png`, `en-tiny-rejected.png`, `en-async-confirm.png`, `en-sync-saved.png`, `en-async-saved.png`, `zh-async-saved.png`.
- `en-invalid.png`, `zh-CN-legacy.png`, `zh-CN-invalid-null.png`; remaining bilingual legacy/invalid/null PNGs are also retained.
- `zh-390-async-model.png`, `zh-390-deadline-rejected.png`, `zh-390-async-confirm.png`, `zh-390-async-saved.png`.
- `en-390-async-model.png`, `en-390-async-confirm.png`, `en-390-async-saved.png`.

Backgrounds: browser detail/initial wizard harness `bg-0b3468aa-6357-4494-beb9-3607207b7fc0` exit 1 (fixture issue above); wizard-only correction `bg-16aed23b-2615-44a8-b958-24ea912f1bbd` exit 0.

## Failures, known risks and cleanup

- **Known UI layout FAIL/exception, not fixed:** at innerWidth=390 the new-wizard document scrollWidth is **562px (en)** / **439px (zh-CN)**. Steps wrap/crop and horizontal scrolling can shift the page; English detail tabs also overflow. This is the pre-existing full-page narrow-layout issue identified in the handoff. Mode controls, deadline errors and confirmation still function with keyboard, but this is not an unqualified responsive/accessibility approval. Screenshots above show the actual clipping. Return to a separately authorized layout fix node if required; no product workaround was applied.
- **Harness failures, not hidden product passes:** initial one-shot metadata 503 failed the test because React StrictMode immediately made a second successful GET; corrected only the fixture control and reran the affected wizard. Earlier manual attempts used an incorrect wait string / a URL glob and one foreground wait timed out; subsequent actual snapshots/readbacks and `wait --fn` proved navigation. `agent-browser fill` cleared the DOM number field without changing the submitted React value in this environment; corrected test input to real keyboard events, then asserted the actual `null`/tiny/valid payload behavior. These failed attempts remain in the transcript/logs.
- Mock injected HTTP 503s exercise generic transport-failure retry (displayed `Service Unavailable`), **not** server error-envelope localization. Deadline and invalid-mode localized text was checked directly in both languages. No claim of broader API-localization coverage is made from these injected failures.
- All own browser resources stopped: `browser_cli.py WORK close` returned **Browser closed**; sent TERM to the owned service supervisor PID **1072507**; managed task `bg-794ef502-7ddc-446e-b6ab-0ef3c13a39b9` reached **exit 0**. Reaped Chrome PID 1072544 (0), Vite PID 1072543 (143), mock PID 1072542 (-15). Final `ps` found none of those PIDs and `ss -ltnp` found **no listeners on 5187, 18187, 42065**. No unrelated browser/session was stopped.
- Retained for reproduction: private CPU environment/cache (~1.6 GiB), source archive, logs/screenshots in `/tmp/tp-colocate-qa-1012v1`, and isolated browser profile under `~/snap/chromium/common/tp-colocate-qa-1012v1`. These are local test artifacts, not real config or shared source cache; they may be removed by their owner after review. No commit or task archive was attempted.
- The task remains `in_progress`. AC5/AC6 (actual cloud trainer scheduling, valid trajectories/non-padding rows/optimizer/checkpoint and billed-resource cleanup) remain **unverified and unauthorized**. Queue restoration stays unsupported by pinned TQ 0.1.8; per-attempt deadline is not a whole-Run or billing cap. A separate approved GPU test is still required before claiming real-training acceptance.

Changed/added in this worker: `verification.md`, `prepare_cpu.py`, `framework_contract.py`, `mock_api.py`, `browser_services.py`, `browser_cli.py`, `browser_acceptance.py`, `summarize_artifacts.py`, all inside this task. No new dependencies were added.

---

# Historical handoff (superseded by the results above)


# Verification — colocate_async

## Backend implementation scope

Backend worker: router/service/renderer/preflight and a new hermetic test module only. Existing uncommitted Git-source migration is preserved; no dependencies, lockfiles, ORM, planner, engine/recovery mechanism, training resource counts, or real services are changed. Pure mode/batch/profile helpers live below services to avoid render → services → render imports. Parent owns frontend/locales, full make verify, fixed-framework checks and broader acceptance.

## Requirement-to-test checklist

- Request default / strict enum / preview / new spec / reopened detail: pending.
- Invalid requests and params/plan injection: no Run/Job/image/capacity side effects: pending.
- Missing legacy mode only falls back; invalid stored values fail: pending.
- B/m positive strict integers and divisibility; mini null inheritance; legal other zeros: pending.
- Two modes × FSDP/Megatron full/LoRA, native async settings, LoRA/config-name/security/resource invariants: pending.
- Async finite hours (existing range, ≥1 rendered second), sync optional hours, final manifest deadline: pending.
- Preflight early rejection; retry/manual resume/backend restart retain original spec and mode: pending.

## Results

Parent recovery after the backend workflow node was terminated: the backend edits and appended cases in `backend/tests/test_runs.py` exist and parse successfully. The parent ran `UV_OFFLINE=1 HF_HUB_OFFLINE=1 AWS_EC2_METADATA_DISABLED=true uv run --locked --offline pytest tests/test_runs.py tests/test_engine.py -q`; it reached terminal exit 0 (task bg-87bc8527-16b9-4a16-bae4-810ec20c8df1). A Starlette/httpx deprecation warning remains.

Targeted Ruff check passed. Ruff format --check found formatting-only differences in the appended test_runs.py cases; the exact read-only formatter diff was inspected, and final correction remains pending. No formatter mutation or product behavior workaround was performed by the parent. Real Hydra compose, SDK CPU contracts, frontend/browser and full make verify remain pending.

Current backend handoff: canonical mode/type/map lives in render.train, service composes validation (no reverse import cycle). `CreateRun.training_mode`, `preview.training_mode`, and `Run.spec.training_mode` are the public data path. New domain codes to localize: `run.invalid_training_mode`, `run.invalid_batch`, `run.async_deadline_required`; the existing `run.bad_plan_override` also covers invalid profile/strategy pairs. Async deadline validation occurs at API/preflight/image/capacity/render boundaries; manual resume validates before changing the ledger. Missing legacy mode is sync, explicit invalid values are rejected. Local tests only; no live 8100 write/restart, AWS/K8s/HF, build or GPU operation was run.

## Handoff / limitations

New error codes and exact check outputs to be added after implementation. String-level Hydra assertions are not actual fixed-verl compose or GPU validation. RayJob deadline is per attempt, not a total Run time/cost ceiling.
