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
- `backend/pyproject.toml` consumes the editable sibling checkout at
  `../agentcore-rl-toolkit` relative to the repository root. Its actual uv source path is
  `../../agentcore-rl-toolkit`, relative to `backend/`. Do not copy toolkit code into this app.
- `backend/app/core/config.py:Settings` resolves runtime paths. `data/`, `.run/`, and
  `config/tuningpad.yaml` are local state/configuration, never new source directories.

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
