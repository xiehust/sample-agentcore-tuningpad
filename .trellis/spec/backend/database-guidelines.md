# Database Guidelines

> SQLAlchemy 2 ledger on SQLite; AWS and Kubernetes remain the source of resource truth.
> Implementation: `backend/app/core/db.py` and `backend/app/models.py`.

## Engine and configuration

`backend/app/core/config.py:Settings.db_url` uses `database_url` when set, otherwise
`sqlite:///<data_dir>/tuningpad.db`. `get_settings()` is cached; configuration precedence
is defaults < YAML < `TUNINGPAD_*` environment < constructor arguments.

`backend/app/core/db.py:init_db(url=None)` creates the SQLite parent directory, builds the
engine, imports models, and creates/extends the schema. `_make_engine()` configures
SQLite with `check_same_thread=False`, a 30-second timeout, WAL journaling, and
`PRAGMA foreign_keys=ON`. This permits the threaded job engine to use separate sessions;
it does not make a shared `Session` safe across threads.

The ledger stores identifiers, submitted specs, stage progress, metrics, and log cursors.
Do not treat a stored status as proof an AWS/K8s object exists. Pipeline recovery rules
are in [AWS/K8s operations](./aws-k8s-operations.md).

## Model declarations and identifiers

Declare tables in `backend/app/models.py` with `Mapped[...]` and `mapped_column()`:

- `Base` in `core/db.py` subclasses `DeclarativeBase`; its annotation map maps plain
  `dict` and `list` to JSON. Models also explicitly use `mapped_column(JSON, ...)`.
- Resource models inherit `Base, TimestampMixin`. The mixin adds `created_at`, `updated_at`
  with UTC Python defaults, and indexed `workspace_id` defaulting to `DEFAULT_WORKSPACE`
  (`"default"`). `utcnow()` supplies timestamps; `updated_at` has an ORM `onupdate` default.
- `RunMetric` is the exception: it inherits only `Base`, has an autoincrement integer ID,
  and no workspace/timestamp columns. Do not claim the mixin is present on every table.
- Resource IDs are string primary keys; callers use `new_id(prefix)` (prefix plus ten
  UUID hex characters). Tables are mostly plural snake_case (`agent_runtimes`, `runs`);
  `Project.__tablename__` is the singular `project`.
- Relationships are explicit foreign-key columns, not ORM `relationship()` navigation.
  For example, `Run.agent_runtime_id` references `agent_runtimes.id`.
- Keep actual uniqueness contracts: `AgentRuntime` has `(agent_id, cluster_id)`, and
  `RunMetric` has `(run_id, step, key)`. Cascade deletion is specified only on some FKs;
  inspect the model before assuming all dependent rows disappear automatically.

`workspace_id` is a future-extension field, **not implemented tenant isolation**.
Current queries generally do not filter by it, and
`backend/app/services/project.py:PROJECT_ID` is the singleton `"default"`.
Do not expose multi-tenant access on the assumption that the column enforces scoping.

## Session and query patterns

Use `backend/app/core/db.py:session_scope()` for current route/service/stage code.
It yields a new session, commits on success, rolls back on any exception, and always
closes. `get_session()` wraps it as a FastAPI dependency, but most existing routes use
the context manager directly. The sessionmaker uses `expire_on_commit=False`.

The existing query style is `s.get(Model, id)` for primary-key lookup and
`s.query(Model).filter(...).order_by(...).all()` for lists. See
`backend/app/routers/jobs.py:list_jobs()` and `backend/app/jobs/engine.py:latest_job()`.

```python
with session_scope() as s:
    job = s.get(Job, job_id)
    if not job:
        raise NotFound("job.not_found", f"job {job_id} not found")
    return job_view(job)
```

`backend/app/services/project.py:get_project()` uses `s.flush()` to materialize defaults
before returning a newly added row's view. Keep session blocks short: routes commit new
resources before `get_engine().start()`, and stages reopen sessions for progress writes.
`backend/app/pipelines/run.py:load_run()` returns a plain dictionary for subsequent work.
Do not keep a transaction open through a capacity wait or share the session with a worker.

## JSON columns and durable stage outputs

JSON shapes belong to their writers: `Run.spec`/`compute` describe a run, `Run.progress`
contains cursors and progress, and `Job.payload`/`context`/`stages` hold pipeline state.
Defaults use callables (`default=dict`, `default=list`), not shared mutable objects.
There is no `MutableDict`/`MutableList` instrumentation: replace JSON values on update.

- `backend/app/services/project.py:save_region()` copies `p.regions`, merges the region's
  entry, then assigns `p.regions = regions`.
- `backend/app/pipelines/run.py:save_run()` merges and reassigns `Run.progress` so a partial
  update does not discard log cursors or other keys.
- `backend/app/jobs/engine.py:StageContext.set()` updates the in-memory context and commits
  a newly assigned `Job.context` dictionary. Use `ctx.set()` rather than only changing
  `ctx.context`; `_update_stage()` similarly copies and reassigns the stages list.
- `backend/app/pipelines/run.py:_ingest_metrics()` queries `(run_id, step, key)`, updating
  an existing value or inserting a new row. Re-reading a log must not duplicate metrics.

## Schema evolution: additive nullable columns only

There is no Alembic configuration or versioned migration framework. `init_db()` calls
`Base.metadata.create_all()` and then `_add_missing_columns()`; `create_all()` alone
never changes an existing table.

`_add_missing_columns()` inspects each table and emits `ALTER TABLE ... ADD COLUMN`
with the compiled type for missing nullable, non-primary-key model columns only.
It does not backfill data or add model defaults, foreign keys, indexes, or uniqueness
constraints to those added columns. It does not rename/drop columns or alter types.

For an additive change to an existing resource, declare the field nullable, for example
`AgentRuntime.platform_version: Mapped[str | None]`. Handle `None` in readers for old rows.
Do not add a required column and expect startup to upgrade existing installations.
A required field, constraint change, or backfill needs an explicitly designed upgrade;
there is no existing migration command that performs it.

## Verification and common mistakes

- **Testing only a fresh DB:** in `backend/tests/test_agents_datasets.py`,
  `test_init_db_adds_new_nullable_columns()` constructs an old table, reruns `init_db()`,
  and verifies new nullable columns appear while NOT NULL `last_smoke` is not added.
  Add equivalent upgrade coverage alongside new fields.
- **Mutating JSON in place:** `row.progress["step"] = n` may not persist. Follow `save_run()`
  and assign a merged dictionary, then assert the value after reopening a session.
- **Starting a job before its target commits:** a worker uses a different session and can
  miss the row. Preserve the commit-before-start ordering in `routers/runs.py:create()`.
- **Assuming every FK cascades:** `routers/runs.py:delete()` explicitly deletes metrics;
  other dependents can still prevent deletion. Review FK definitions, not table names.
- **Using local operator data in tests:** `backend/tests/conftest.py:_isolated` rebinds to a
  temporary SQLite database for every test. Never test upgrades against `data/tuningpad.db`.
