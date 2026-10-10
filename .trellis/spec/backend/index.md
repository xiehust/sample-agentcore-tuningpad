# Backend Development Guidelines

> Source-backed conventions for TuningPad's FastAPI control plane and staged job engine.
> Read `AGENTS.md` first; all paths in these guides are relative to the repository root.

## Overview

The backend is `backend/app/`, with a hermetic suite in `backend/tests/`.
`main.py:create_app()` composes HTTP routes, auth, error handling, and background lifecycle;
`jobs/engine.py:JobEngine` runs resumable pipelines over AWS/Kubernetes resources.
SQLite is a local ledger, not the source of truth for external resource state.

These guides describe implemented behavior and identify its limits: schema evolution is
additive nullable columns, workspace IDs do not implement tenant isolation, and logs are
plain text without central redaction. Do not assume a migration system, structured
logging service, or automatic exactly-once execution exists.

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Index](./index.md) | Backend overview, guide inventory, pre-development and quality checklists | Filled |
| [Directory Structure](./directory-structure.md) | App lifecycle, route/pipeline registration, module boundaries, and asset ownership | Filled |
| [Database Guidelines](./database-guidelines.md) | SQLAlchemy sessions/models, JSON reassignment, workspace limits, nullable-column schema upgrades | Filled |
| [Error Handling](./error-handling.md) | AppError envelope/status mappings, localized codes, recovery, and persisted job failures | Filled |
| [Quality Guidelines](./quality-guidelines.md) | Python/uv/Ruff, hermetic stubs, make verify, and cost/security review checks | Filled |
| [Logging Guidelines](./logging-guidelines.md) | Process diagnostics, job byte-offset logs, S3 training logs, and secret-handling limits | Filled |
| [AWS / K8s Operations](./aws-k8s-operations.md) | AgentCore V1/V2, cluster delete order, GPU node lifecycle, EFA proof, CPU-node CUDA images, eval sampling | Filled |
| [Image-only AgentCore Updates](./agentcore-image-updates.md) | Immutable network fields, preserved configuration, update idempotency, DEFAULT routing and readiness-only evidence | Filled |

## Pre-Development Checklist

- Locate the matching router, service, and pipeline using [directory structure](./directory-structure.md).
  Router registration and pipeline import registration are separate operations.
- For model/progress changes, read [database guidelines](./database-guidelines.md) before
  editing `backend/app/models.py` or `backend/app/core/db.py`.
- For any new failure, check [error handling](./error-handling.md), including both locale
  files referenced there; English fallback text alone is not a localization contract.
- For staged/external work, read [AWS/K8s operations](./aws-k8s-operations.md) and the
  `StageContext` contract in `backend/app/jobs/engine.py`. Decide what must survive restart.
- Inspect the existing test fixture in `backend/tests/conftest.py` before adding AWS/K8s
  calls; never use real credentials or GPU provisioning for ordinary verification.
- For diagnostics, distinguish engine logs from S3 training output using
  [logging guidelines](./logging-guidelines.md); inspect every value for sensitive content.

## Quality Check

Before considering a backend change complete:

- Run `make verify` from the root. `scripts/verify.sh` covers backend Ruff/pytest, lifecycle
  and asset syntax, frontend lint/build, and locale parity; see [quality guidelines](./quality-guidelines.md).
- Add regression coverage alongside changed behavior, including failed/retried stages,
  JSON persistence, or old-schema startup where applicable.
- Confirm `aws.client()`/K8s factory usage, resource tags, stable error codes, and
  import/route registration. Verify new codes in both English and Chinese locales.
- Preserve explicit cost confirmation, guardian protection, configured RayJob deadlines,
  loopback/auth restrictions, service-account trust scoping, and AgentCore-only ingress.
- Check the diff excludes secrets and local `config/tuningpad.yaml`, `data/`, and `.run/`.
  No live cloud action is authorized merely by running this checklist.

## Common mistakes

- **Reading only this index:** detailed contracts and known operational failures belong
  in the linked guides; this file is navigation, not a replacement for stage-specific rules.
- **Treating aspirational architecture as existing code:** request schemas currently live
  in routers, queries use `session_scope()`, and schema upgrades use `_add_missing_columns()`.
  Cite the implementation when extending a convention rather than inventing a parallel API.
- **Verifying only a happy-path HTTP response:** jobs complete asynchronously and external
  effects must be idempotent. `backend/tests/test_engine.py` and `test_setup.py` show the
  persisted-state/retry checks expected in addition to API status assertions.

**Language**: Keep backend guidelines in English and update source/test references when
implementation contracts change.
