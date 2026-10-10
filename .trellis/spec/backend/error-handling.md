# Error Handling

> Stable error codes connect the FastAPI API, background jobs, and localized console.
> The HTTP boundary is `backend/app/core/errors.py:register_error_handlers()`.

## Domain errors and their contract

Raise `AppError(code, message, *, status=None, detail=None)` for an expected operator or
resource failure. `backend/app/core/errors.py` defines these defaults:

| Exception | Default HTTP status | Use |
|---|---|---|
| `AppError` | 400 | Invalid operation or domain input; optional explicit status override. |
| `NotFound` | 404 | Missing ledger/resource object, e.g. `job.not_found`. |
| `Conflict` | 409 | Resource state conflicts with the requested operation. |
| `Unauthorized` | 401 | Authentication failure. |

Use a stable dotted code for new domain failures, not an AWS message or object ID as the
code. Keep `message` useful in English and put structured context in JSON-serializable
`detail`. Existing built-in fallback codes such as `internal` are exceptions to the
dotted naming convention, not a reason to introduce more ad hoc names.

For example, `backend/app/services/project.py:require_region()` reports an unmet
prerequisite without turning it into an unexpected internal error:

```python
raise AppError(
    "setup.region_not_ready",
    f"project setup has not completed in {region}; run Settings → Project setup first",
    status=409,
    detail={"region": region},
)
```

## HTTP envelope and validation matrix

`backend/app/main.py:create_app()` installs the handlers. Responses have exactly the
application envelope shape `{code, message, detail}`; `detail` is present even when null.
Do not wrap it in FastAPI's usual `{"detail": ...}` shape in new domain code.

| Raised condition | HTTP status | Envelope code / detail |
|---|---|---|
| `AppError` subclass | Its `status` | Its `code`, `message`, and `detail`. |
| Botocore `ClientError`: `AccessDenied` or `AccessDeniedException` | 403 | `aws.client_error`. |
| Other botocore `ClientError` | 502 | `aws.client_error`. |
| Botocore `BotoCoreError` | 502 | `aws.unavailable`, exception text as message. |
| FastAPI `RequestValidationError` | 422 | `request.invalid`, encoded validation errors in detail. |
| Starlette `HTTPException` | Exception status | `http.<status>`, stringified exception detail as message. |
| Unexpected exception | 500 | `internal`, message `internal error`, detail `{"type": <class name>}`. |

`aws_error_detail()` exposes only `aws_code`, `aws_message`, and `operation` from a
`ClientError`; it does not return the whole SDK response. Validation errors are encoded
with `jsonable_encoder(..., custom_encoder={Exception: str})`, because Pydantic validators
may put an exception object in `ctx`. Preserve that serialization behavior.

`backend/app/core/auth.py:AuthMiddleware` returns equivalent JSON envelopes directly for
`auth.required` (401) and `auth.loopback_only` (403). It does not rely on raising from
middleware into the route exception handlers. `validate_startup()` instead raises
`RuntimeError` to refuse unsafe startup; that is not an HTTP response path.

## Localization is part of adding a code

`frontend/src/lib/api.ts:parseResponse()` turns non-2xx envelopes into `ApiError` carrying
`code`, `detail`, and HTTP `status`. `localizedMessage()` checks `apiErrors.<code>` and
falls back to the backend English message if the key is missing.

Job failures arrive in successful job-query responses as nullable `Job.error_code` and
`Job.error`, so they do not pass through `parseResponse()`'s error branch. `JobPanel`
reuses `localizedMessage(code, fallback)` during rendering: show the localized summary
and keep the original code and diagnostic text; show the diagnostic only once if it is
already the displayed message. Unknown/null codes fall back to the raw error; null/empty
errors produce no alert. Do not overwrite the stored diagnostic with a translation or
cache the translated summary in state, which would make language switching stale.
For this path, verify both locales, live language switching, unknown/empty codes,
identical summary/raw text, and long diagnostics in mock-only browser QA.

Every new user-facing code needs matching entries in both:

- `frontend/src/locales/en/common.json`
- `frontend/src/locales/zh-CN/common.json`

Under `apiErrors`, the JSON files use literal dotted property names, for example
`"auth.loopback_only"`. Preserve that existing layout. Run
`python3 scripts/i18n_check.py` for locale key parity; parity alone does not prove every
backend error code has a translation, so review newly introduced codes explicitly.

## Recovery and background job failures

Catch external exceptions only where the operation has a defined recovery path.
`backend/app/services/kube.py:get()` returns `None` for not-found/404 and re-raises other
API failures. `backend/app/services/runs.py:read_log()` treats `NoSuchKey`, `InvalidRange`,
`404`, and `416` as no log chunk yet, but propagates other `ClientError`s. Do not convert
access denial or transport failure into proof that a resource is absent.

Background work does not pass through FastAPI exception handlers. In
`backend/app/jobs/engine.py:JobEngine._run()`:

- `JobCancelled` marks the job cancelled without an error-code failure. A stage is marked
  cancelled only if the exception comes from its function; cancellation checked between
  stages leaves their existing statuses unchanged.
- `AppError` marks the job failed with its `message` and `code`; truthy `detail` is written
  to the job log. The HTTP status is not part of the persisted job result.
- Other exceptions write a traceback and store `<type>: <message>`. The current fallback
  code is `aws.client_error` when the exception class name is `ClientError`, otherwise
  `internal`; do not assume all HTTP mappings also exist in the worker.
- `StageContext.wait_until()` raises `AppError("job.timeout", ...)` on a polling timeout.
- `JobEngine.retry()` clears the job error and re-runs unfinished stages, not succeeded
  stages. `job_view()` exposes `error` and `error_code` to the console.

`JobEngine._finish()` runs the finish hook before persisting terminal job status. Hook
and reconciler exceptions are logged with `log.exception()` so they do not kill engine
bookkeeping/the reconciler loop. This is an explicit containment boundary, not a pattern
for silently swallowing errors in a provisioning stage.

## Tests and common mistakes

- **Returning a success body containing an error:** raise a domain exception so HTTP
  status and envelope agree. `backend/tests/test_core.py:test_error_envelope` asserts
  `job.not_found` and `http.404`; extend API tests for new failure branches.
- **Expecting API handlers to catch worker failures:** assert stored job code/status as
  `backend/tests/test_engine.py:test_failure_records_code_and_retry_resumes_from_failed_stage`
  does. Also test cancellation and `job.timeout` when changing polling behavior.
- **Replacing Pydantic validation with generic 500s:** `routers/runs.py:Compute._ec2_capacity()`
  raises `ValueError` inside a model validator; invalid requests belong to the 422 path.
- **Logging/returning secrets in error context:** `AppError.detail` is returned over HTTP
  and copied to job logs. There is no automatic redaction layer; supply only safe context.
- **Inventing broader retry rules:** preserve known absence/transient handling and consult
  [AWS/K8s operations](./aws-k8s-operations.md) for actual recovery cases.
