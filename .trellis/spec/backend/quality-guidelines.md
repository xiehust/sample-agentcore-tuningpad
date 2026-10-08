# Quality Guidelines

> The gate is `make verify`; the test suite must never provision real AWS/K8s resources.
> Sources: `AGENTS.md`, `backend/pyproject.toml`, and `scripts/verify.sh`.

## Toolchain and formatting

- Use Python 3.12-compatible code. `backend/pyproject.toml` declares
  `requires-python = ">=3.12"` and Ruff targets `py312`.
- Manage the environment with `uv`, not pip. `make install` runs `uv sync` in `backend/`
  (and npm install for the console). Use `uv run` for Python checks.
- Published runtime/dev dependencies are pinned with exact `==` versions. Preserve
  those pins and the lockfile when changing dependencies. The deliberate exception is
  `agentcore-rl-toolkit`, an editable path source at `../../agentcore-rl-toolkit` relative
  to `backend/pyproject.toml`; do not replace it with an uncoordinated package release.
- Ruff enforces `E F I W UP B` with line length 100, plus `ruff format --check`.
  Existing E501 exceptions are `tests/**`, `app/render/train.py`, and
  `app/services/serving.py`. Do not broaden ignores to hide a new failure.
- Ruff's Bugbear configuration accepts `fastapi.File`, `Form`, `Query`, and `Depends`
  calls in signatures. Follow those existing request/dependency patterns.
- There is no Python type-checker step in the canonical gate; `tsc` checks the frontend.
  Still preserve the existing Python annotations (`Mapped`, dataclasses, typed helpers).

## Canonical verification

Run from the repository root:

```bash
make verify
```

`scripts/verify.sh` aggregates failures across these sections and exits nonzero if any
section fails; a successful pytest invocation alone is not the full gate:

1. Backend `uv run ruff check .` and `uv run ruff format --check .`.
2. Backend `uv run pytest -q -n auto` (pytest-xdist).
3. Ruff and Python compilation for `start.py`, plus `bash -n stop.sh`.
4. `bash -n` for shell files in `cluster_assets/` and `trainer_image/`.
5. Frontend ESLint, then `tsc --noEmit && vite build` through the npm build script.
6. `scripts/i18n_check.py` for English/Chinese key parity. Its optional `--strict` mode
   additionally scans hardcoded strings; the standard gate does not enable it.

For quick feedback use `cd backend && uv run pytest tests/test_runs.py -q`, or
`cd backend && uv run ruff check . && uv run ruff format --check .`.
Run the full gate before completion, and report failures rather than weakening checks.
Do not start the application against operator credentials as a substitute for tests.

## Hermetic fixtures and stubs

`backend/tests/conftest.py:_isolated` is autouse. For every test it:

- Sets a temporary `TUNINGPAD_DATA_DIR`, points `TUNINGPAD_CONFIG_FILE` to an absent file,
  removes `TUNINGPAD_PASSWORD`, clears cached settings, and binds a temporary SQLite DB.
- Installs AWS and K8s factories that raise on unexpected use. This blocks clients through
  the factories, not arbitrary networking/subprocesses; explicitly stub other I/O too.
- Installs a two-worker `JobEngine`, then shuts it down and resets factories/settings
  at teardown. `client` uses `TestClient(create_app(start_background=False))`.

Use the provided `stub_aws` fixture and `StubClient`, not a real boto3 client. The stub
maps method names to return values or callables, records `(method, kwargs)` in `.calls`,
and raises for missing methods/services. Positional calls are recorded under `_args`.
For example, `backend/tests/test_agents_datasets.py` stubs S3 uploads this way:

```python
from tests.conftest import StubClient

s3 = StubClient(upload_file=None)
stub_aws({"s3": s3})
```

Assert meaningful call arguments and final ledger state, not just a 200 response.
`backend/tests/test_setup.py:test_setup_region_creates_everything` checks S3 creation,
IAM trust conditions, and persisted region readiness. Its `test_setup_is_idempotent`
checks that existing resources are not created again.

## Coverage to add alongside changes

- Routes/errors: assert status and stable code; use `test_core.py:test_error_envelope`
  as the baseline. New user-facing codes require both locale entries; see
  [error handling](./error-handling.md).
- Jobs: cover ordered stages, persisted `ctx.set()` output, failure/retry without
  replaying succeeded stages, restart recovery, and cancellation. Existing examples
  are in `backend/tests/test_engine.py`; `JobEngine.start(run_inline=True)` is available
  for deterministic execution when asynchronous behavior is not under test.
- Database: reopen sessions to check JSON persistence, and test an old schema as well
  as fresh creation; see [database guidelines](./database-guidelines.md).
- Rendering/planning: assert actual Hydra keys, manifest fields, node counts, defaults,
  and overrides, following `backend/tests/test_runs.py` and `test_planner.py`.
- Cost/security: use `test_cluster.py`, `test_nodegroups.py`, and `test_guardian.py` to
  exercise confirmation, scoped permissions, busy capacity, idle shutdown, and budgets.

## Required boundaries and guardrails

Use `backend/app/core/aws.py:client()` and `backend/app/core/k8s.py` factories only.
Never create `boto3.client()`/independent SDK sessions in feature code; that bypasses
caching, retry/auth policy, and hermetic stubs. Prefer `services/kube.py:apply()` for
owned manifests; it uses the `tuningpad` field manager and managed-by label.
Tag created AWS resources with `aws.tags()` or `aws.tag_map()` (Project/ManagedBy plus
resource-specific tags), matching the receiving API's list/map shape.

Preserve these concrete protections; see [AWS/K8s operations](./aws-k8s-operations.md)
for additional operational constraints:

- `routers/runs.py:CreateRun.confirm_cost` and cluster/nodegroup route confirmations
  must remain explicit before GPU scale-up. Never provision billable capacity for tests.
- `services/components.py:guardian_cronjob()` and `cluster_assets/guardian.py` are the
  independent idle/budget backstop; do not make protection depend only on the backend.
- `services/runs.py:render()` translates a supplied `compute.max_hours` into seconds for
  `render/train.py:rayjob()`'s `activeDeadlineSeconds`. It is currently conditional on
  a configured limit, not unconditional; preserve this enforcement when a limit is set.
- `core/auth.py:validate_startup()` and `AuthMiddleware` prevent unauthenticated remote
  access; preserve the loopback-only default without `TUNINGPAD_PASSWORD`.
- `services/components.py:ensure_security_groups()` restricts gateway 18765 and vLLM
  8000 access to the AgentCore SG (and the NLB-to-pod path); do not open public ingress.
- `render/train.py:agent_loop_yaml()` sets `require_registered_sessions=True`.
  `services/components.py:irsa_trust()` scopes trust to the service-account subject
  and STS audience; retain workload/guardian policy scoping as well.
- Never commit `config/tuningpad.yaml`, `data/`, or `.run/`.

## Common mistakes and review checklist

- **Trusting a fresh schema or in-memory JSON mutation:** verify reopened persistence and
  old-DB compatibility; `create_all()` is not a general migration system.
- **Suppressing unexpected clients in tests:** a missing stub is a test failure to fix,
  not permission to use real AWS. Keep `_NoAws` and the K8s rejecting factory intact.
- **Ignoring restart semantics:** register stable stage names, inspect real state, and
  use `ctx.set()` for durable outputs. The interrupted Helm regression is covered by
  `backend/tests/test_core.py:test_helm_clears_release_left_pending_by_interrupted_run`.
- **Changing defaults without contract tests:** `render/train.py:merge_params()` must
  preserve explicit zero values and interpret cleared/null values correctly; see
  `test_runs.py:test_cleared_params_restore_defaults_and_explicit_zero_is_preserved`.
- **Calling done after only a focused test:** review the full diff for auth/cost changes,
  error-code translations, untagged resource creation, secrets in logs, and run the gate.
