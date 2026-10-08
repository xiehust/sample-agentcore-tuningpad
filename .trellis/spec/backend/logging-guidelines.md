# Logging Guidelines

> Three separate streams exist: Python process diagnostics, durable job logs, and
> training output in S3. There is no application-wide structured logging framework.

## Process diagnostics: actual setup

The application uses Python's standard `logging` module in three places:

| Source | Logger and current usage |
|---|---|
| `backend/app/main.py` | `logging.getLogger("tuningpad")`; INFO for the number of resumed jobs. |
| `backend/app/core/errors.py` | `logging.getLogger(__name__)`; `log.exception("unhandled error")`. |
| `backend/app/jobs/engine.py` | `logging.getLogger(__name__)`; exception tracebacks for failed reconcilers and finish hooks. |

There is no `basicConfig()`, application logging `dictConfig()`, JSON formatter,
request-correlation middleware, or application log rotation setup. `create_app()` does
not install logging handlers/levels; visibility depends on the process runner's logging
configuration. Do not describe INFO messages as guaranteed visible under every runner.

`start.py:_spawn()` redirects each child process's stdout/stderr into `.run/<name>.log`
(the backend is `.run/backend.log`). `make backend` runs Uvicorn directly instead.
These files are distinct from job logs and are gitignored runtime artifacts.

No DEBUG/WARNING level taxonomy is established in backend application code. Follow the
existing division: use process diagnostics for internal engine/server failures and
`StageContext.log()` for operator-visible pipeline work. Use `log.exception()` inside
an exception handler when a traceback is needed; do not add duplicate logging to each
caller of a handled `AppError`.

## Durable job log contract

`backend/app/jobs/engine.py` owns these APIs:

- `log_path(job_id)` resolves `<Settings.data_dir>/jobs/<job_id>.log`, creating the jobs
  directory. The default data directory is `data/` from `core/config.py:Settings`.
- `append_log(job_id, message)` appends UTF-8 text, prefixing every line with a UTC
  timestamp formatted as `%Y-%m-%dT%H:%M:%SZ`. It does not assign severity levels.
- `StageContext.log(message)` adds `[<stage name>]` to the message before appending it.
- `StageContext.detail(text)` persists a short progress message in `Job.stages` for the
  stage bar; it is not a replacement for durable log output.
- `read_log(job_id, offset=0, limit=LOG_READ_LIMIT)` reads **bytes**, not characters.
  `LOG_READ_LIMIT` is 256 KiB; output is `{content, next_offset, eof}`.

`read_log()` clamps the offset into the file's size, decodes with UTF-8 replacement,
and returns empty content/offset 0/EOF for a missing file. A split multibyte character
can be replaced at a chunk boundary; clients must use the returned byte offset rather
than deriving a new one from the decoded string length.

`backend/app/routers/jobs.py:get_log()` serves this through
`GET /api/jobs/{job_id}/log?offset=...`. The endpoint delegates directly to the log reader;
it does not first check for a `Job` row. Do not assume missing logs produce a 404.
There is no engine-side retention/rotation policy for these local append-only files.

## What belongs in a job log

`JobEngine.start()`, `_run()`, `retry()`, `cancel()`, `resume_pending()`, and `_finish()`
already log lifecycle boundaries: queueing, stage start/done, retry/cancel requests,
resume, and terminal status. A stage should add the operational details the user needs:

- Object identifiers and progress: `pipelines/run.py:stage_image()` logs the trainer
  image URI; `_submit()` logs the RayJob name.
- Wait/retry reasons: `pipelines/run.py:stage_monitor()` logs failed/missing RayJobs,
  retry counts, and checkpoint resume; its current state/cost goes to `ctx.detail()`.
- Output locations and counts: `pipelines/dataset.py:_write()` logs split row counts
  and S3 URIs, not the full training dataset.
- Cleanup outcome: `pipelines/run.py:_release_rayjob()` logs release or a skipped cleanup
  reason through its supplied callback.

Service helpers can accept a log callback rather than importing the engine. For example,
`backend/app/services/agents.py:_run()` streams subprocess output through the passed
callback; stages pass `ctx.log`. A parameter named `log` in those services is not a
Python `Logger` and has no `.info()` or `.exception()` methods.

## Errors and sensitive values

`JobEngine._run()` writes truthy `AppError.detail` into the job log and records its
message/code on failure. Unexpected exceptions write `traceback.format_exc()` plus the
exception type/message. HTTP unexpected errors instead expose only the type to clients
and send the traceback to the process logger (`core/errors.py`). See
[error handling](./error-handling.md) before adding diagnostic context.

There is **no central redactor**. Avoid passwords, session cookies, bearer tokens, AWS
credentials, endpoint API keys, or wholesale request/settings/secret-manifest dumps in
messages and exception details. Existing code demonstrates selective handling:

- `services/agents.py:ecr_login()` passes the ECR password to Docker via `--password-stdin`;
  `_run()` logs output, not `input_text`. Preserve that separation.
- `services/serving.py:secret_manifest()` stores the endpoint key in a Kubernetes Secret;
  `pipelines/serving.py:stage_deploy_endpoint()` logs the model/URL, not the key.
- `services/agents.py:_run()` includes the last 40 subprocess-output lines in command
  failure details. Those lines and third-party tracebacks are not guaranteed sanitized;
  never promise that arbitrary child output or exceptions are safe to publish.

## Training logs are a different stream

`backend/app/render/train.py:train_script()` writes the training log under the run's
FSx directory and periodically copies it to `runs/<run_id>/logs/train.log` in S3.
`backend/app/services/runs.py:read_log()` uses S3 byte-range reads; the run router caps
console reads at 256 KiB. This is not `jobs/engine.py:read_log()` or the local job log.

`backend/app/pipelines/run.py:_ingest_metrics()` reads `log_offset` and `log_carry` from
`Run.progress`, parses with `metrics/verl_log.py:parse_chunk()`, and returns updated
cursors (carry capped at 20,000 characters). `stage_monitor()` and `stage_finalize()`
merge those updates via `save_run()`; `_ingest_metrics()` itself persists metric rows,
not the cursors. Preserve that caller-owned persistence when changing log delivery.
Multi-node transport logging is deliberately emitted by `render/train.py:pod_env()`;
see the EFA evidence requirements in [AWS/K8s operations](./aws-k8s-operations.md).

## Common mistakes and verification

- **Using only Python logging for a stage:** operators polling the job endpoint will not
  see it. Use `ctx.log()` and reserve the process logger for internal diagnostics.
- **Advancing by string length:** offsets are bytes. In `backend/tests/test_engine.py`,
  `test_log_offsets()` verifies successive reads do not repeat old lines; retain this test
  and add boundary cases when changing the reader.
- **Dumping full payloads to debug authentication:** neither `append_log()` nor the error
  handlers redact secrets. Log operation names/identifiers and a safe failure reason.
- **Treating callback logs as guaranteed audit records:** cleanup callers sometimes pass
  `lambda m: None` (see `pipelines/run.py:_finished()`). Actual log coverage is selective,
  not a complete audit trail of every AWS/K8s action.
