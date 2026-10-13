# Directory Structure

> TuningPad's synchronous FastAPI control plane, durable job engine, and AWS/K8s adapters.
> Paths below are relative to the repository root.

## Module map

| Location | Responsibility and examples |
|---|---|
| `backend/app/main.py` | `create_app()` wires lifespan, middleware, error handlers, and routes. |
| `backend/app/core/` | Settings, auth, database sessions, error envelope, AWS/K8s client factories. |
| `backend/app/models.py` | SQLAlchemy ledger models: `Cluster`, `Run`, `Job`, and related resources. |
| `backend/app/routers/` | `/api` endpoints, Pydantic request bodies, validation, serialization, job launch. |
| `backend/app/services/` | Domain helpers shared by routes and stages; cloud operations and manifest assembly. |
| `backend/app/pipelines/` | Ordered stages for setup, cluster, agent, dataset, trainer, run, and serving jobs. |
| `backend/app/jobs/engine.py` | `JobEngine`, `Stage`, `StageContext`, pipeline registry, logs, retry/resume. |
| `backend/app/render/train.py` | Training defaults, Hydra overrides, shell/YAML and RayJob rendering. |
| `backend/app/planner.py` | `plan()` chooses training strategy and parallelism; `Plan.to_dict()` serializes it. |
| `backend/app/catalog/` | `instances.py` holds instance specs; `models.py` handles model compatibility. |
| `backend/app/metrics/verl_log.py` | `parse_chunk()` and `summarize()` turn training output into metrics. |
| `backend/app/templates_lib.py` | `get_template()` and `resolve_params()` read and validate agent templates. |
| `backend/tests/` | Hermetic API, service, pipeline, renderer, and guardian tests. |

There is no separate repository/DAO layer or central request-schema package.
Request models live beside their routes, for example `Compute` and `CreateRun` in
`backend/app/routers/runs.py`; persistent models live in `backend/app/models.py`.
Do not confuse those models with the model catalog in `backend/app/catalog/models.py`.

## Startup and registration

`backend/app/main.py:create_app(start_background=True)` obtains cached settings and
constructs the app. Its lifespan validates auth configuration, calls `init_db()`, and
imports pipelines through `_load_modules()`. With background work enabled it calls
`JobEngine.resume_pending()`, starts reconcilers, and shuts the engine down on exit.

- Add ordinary feature routers to `backend/app/routers/__init__.py:register_all()`.
  Auth and job routers are included directly by `create_app()`.
- Add new pipeline modules to `backend/app/pipelines/__init__.py`. They register on import;
  defining a stage without importing its module does not make its job type available.
- `create_app(start_background=False)` still initializes the DB and loads modules during
  lifespan; it only disables automatic resume/reconciler startup and lifespan shutdown.
  Tests use this mode in `backend/tests/conftest.py:client`.

## Keep responsibilities at the existing boundaries

Routes should validate input, access the ledger, call a domain helper or enqueue a job,
and shape the response. Keep new reusable business/cloud logic in `services/` rather
than growing routers. Existing routers do contain validation and SQLAlchemy queries;
this is a convention to follow, not a claim that every route is a one-line wrapper.

The run flow is a useful navigation example:

1. `backend/app/routers/runs.py:create()` validates a `CreateRun`, checks cost confirmation,
   commits a `Run`, and returns its ID plus a job ID from `get_engine().start()`.
2. `backend/app/pipelines/run.py` orchestrates preflight, image, capacity, submit,
   monitoring, and cleanup; `load_run()`/`save_run()` keep ledger access local.
3. `backend/app/services/runs.py:render()` assembles run-specific objects using
   `backend/app/render/train.py`, while `rayjob_state()`/`read_log()` inspect live progress.
4. `backend/app/services/kube.py` owns apply/get/delete/Helm operations, backed by
   `backend/app/core/k8s.py`; AWS calls go through `backend/app/core/aws.py:client()`.

## Adding durable work

Use `stage_*` functions taking `StageContext`, and stable dotted job types. The actual
registration in `backend/app/pipelines/dataset.py` is deliberately small:

```python
register("dataset.ingest", [Stage("ingest", stage_ingest)], on_finish=_done)
```

`backend/app/jobs/engine.py:register()` rejects duplicate stage names. Succeeded stages
are skipped on retry/resume, so names are persisted workflow identifiers, not cosmetic
labels. Stages must inspect real state before creating external resources.

Read prior outputs from `ctx.context`, and write durable outputs with `ctx.set(key, value)`;
plain mutation of the context dictionary is not a persistence operation. Use `ctx.log()`
for operator output, `ctx.detail()` for the stage bar, and `ctx.sleep()`/`ctx.wait_until()`
for cancellable polling. Finish hooks run before the terminal job state is committed and
must tolerate re-execution. See [AWS/K8s operations](./aws-k8s-operations.md) for recovery
and deletion-order contracts rather than duplicating them in a new service.

## Assets and dependencies outside the package

- `templates/` contains `template.yaml` metadata plus agent overlays; these are loaded by
  `backend/app/templates_lib.py`, not embedded in route handlers.
- `cluster_assets/` holds the in-cluster guardian and installation assets;
  `backend/app/services/components.py:install_guardian()` reads `guardian.py` from there.
- `trainer_image/` contains the trainer Dockerfile/build inputs, distinct from the
  runtime manifests generated by `backend/app/render/train.py`.
- `backend/pyproject.toml` owns the toolkit Git URL and full SHA for both installation
  and source consumers. `services/toolkit_source.py` validates installation metadata and
  resolves source on demand; there is no default sibling or editable installation.
  Do not vendor toolkit code into this app. See the contracts below.
- `backend/app/core/config.py:Settings` resolves runtime paths. `data/`, `.run/`, and
  `config/tuningpad.yaml` are local state/configuration, never new source directories.

## Pinned toolkit source contracts

### 1. Scope / trigger

Installation and source consumers must agree with `backend/pyproject.toml`'s Git URL
and full SHA. Source resolution lives in `services/toolkit_source.py`, not routers.

### 2. Signatures

- `source_spec() -> tuple[str, str]`: authoritative URL/revision, no writes/network.
- `validate_installation() -> None`: check Git `direct_url.json` URL/vcs/commit only.
- `inspect_source() -> tuple[Path, bool, AppError | None]`: read-only path/presence/error;
  a malformed declaration raises rather than inventing a path.
- `resolve_source(expected_revision: str | None = None) -> tuple[Path, str]`:
  acquire only a missing default source, then validate.
- `copy_tracked(root: Path, dest: Path, *, expected_revision: str) -> Path`:
  revalidate and copy tracked inputs to an absent private directory.

### 3. Contracts

`Settings.toolkit_path` defaults to `None`. Otherwise it must be a clean worktree root
with the same repository origin (equivalent GitHub HTTPS/SSH identities are accepted),
full HEAD SHA, required files and contained tracked symlinks. No fallback from an invalid
override; never reset/clean a user's checkout. Dirty or editable development is unsupported.

The default source cache is `<data_dir>/cache/toolkit/<full_sha>`, separate from uv's
install/build cache. Cold acquisition uses one fetch, bounded local POSIX locks and Git
commands, then atomic publication; unknown/invalid cache directories are preserved.
These locks are not a multi-host/NFS guarantee. A source-cache hit avoids source fetching,
not dependency downloads during `uv sync`/`uv build`. Imports, health and toolkit diagnostics
never fetch. Other preflight checks still have their existing AWS/ledger effects.

Agent wheel and template inputs share one resolution/private tracked snapshot; builds
never write into the shared root. OfficeBench helper import/pycache also uses a private
snapshot. Trainer `package_source(dest, *, expected_revision)` rejects a mismatched record
before upload. `TrainerImage.toolkit_sha` and existing profile-revision tags use the full
SHA, with no schema/queue/resume or recipe-digest change. Existing cloud images/runtimes
are not automatically updated; later user operations may invoke existing billed builds.

Meta retains `toolkit_path: string` and `toolkit_present: boolean`. Preflight retains
`{id, status, message, detail}`. Only the toolkit row in Settings adds a localized summary
from id/status; it retains the raw message/path and optional `detail.reason` as diagnostics.
No new DTO, source panel or translated path; other rows still show their original message.

### 4. Validation / error matrix

| Condition | Inspect / preflight | Source consumption |
| --- | --- | --- |
| Valid installation + source | present / ok | Reuse, no fetch |
| Valid installation + missing default cache | absent / warn | Fetch pinned SHA on demand |
| Bad metadata, override, dirty/wrong cache, expected SHA mismatch | fail where inspected | `toolkit.source_invalid` |
| Acquisition/local I/O failure | fail if encountered | `toolkit.source_unavailable` |
| Lock/Git/deadline exhausted | fail if encountered | `toolkit.source_timeout` |

Errors carry a bounded technical `detail.reason`; the three codes have paired en/zh-CN
`apiErrors` entries. Inspect does not test connectivity or claim a cold cache is offline.

### 5. Good / base / bad cases

- Good: a clean explicit checkout at the pin is used without mutation.
- Base: a warm default cache is verified and copied into private build inputs.
- Bad: an invalid override is silently bypassed, or ignored build residue is packaged.
  Staged/unstaged/non-ignored untracked inputs fail; ignored residue never enters the copy.

### 6. Required tests

`test_toolkit_source.py` uses temporary Git repositories and metadata stubs for cold/hot,
invalid overrides, concurrency/timeouts and safe-link containment. `test_toolkit_consumers.py`
asserts one Agent resolution/private build, helper cleanup and read-only diagnostic shapes;
`test_trainer_image.py` checks full tags, tracked packaging and expected-revision rejection.
Keep real fetch/build/cloud guards active. Check both locale keys and toolkit-only rendering;
run the full gate separately from real installation preparation, without moving a sibling.

### 7. Wrong vs correct

Wrong: `get_settings().toolkit_path / relative_input` or building in a shared checkout.
Correct: resolve once, `copy_tracked(root, private, expected_revision=revision)`, then
consume private files. For package imports alone, use `validate_installation()` instead
of resolving source. This keeps diagnostics/imports from causing downloads.

## Common mistakes

- **Doing provisioning in app import code:** `main.py` constructs an app at import time;
  initialization and recovery belong to lifespan or stages, not module-level AWS calls.
- **Unregistered features:** both router inclusion and pipeline import registration are
  explicit; inspect `register_all()` and `pipelines/__init__.py` when an endpoint/job is missing.
- **Passing an ORM session into long-running work:** follow `runs.py:create()` and commit
  the target before starting the job; stages open their own short `session_scope()` blocks.
- **Reimplementing adapters in a router:** use `aws.client()`, `k8s` factories, and
  `services/kube.py`; otherwise the test harness cannot reliably intercept cloud access.
- **Duplicating training defaults:** `render/train.py:merge_params()` is authoritative.
  The defaults-display regression is recorded in archived task
  `.trellis/tasks/archive/2026-10/10-07-training-param-defaults/prd.md` and covered by
  `backend/tests/test_runs.py:test_run_defaults_without_runtime_or_cloud_access`.
