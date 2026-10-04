# AGENTS.md — TuningPad

TuningPad is a no-code web console for agentic RL on AWS: a FastAPI control plane (`backend/`) plus a React console (`frontend/`) that drive CloudFormation, SageMaker HyperPod (EKS), Bedrock AgentCore, S3, ECR and CodeBuild. Training is verl GRPO as a RayJob; agents are built on the sibling checkout `../agentcore-rl-toolkit`. See `README.md` for the product workflow.

## Commands

```bash
make install          # backend: uv sync · frontend: npm install
./start.py            # backend :8100 + console :5180 (state in .run/)
./stop.sh
make backend          # uvicorn --reload only
make frontend         # vite dev only
make verify           # the quality gate — must pass before work is done
```

`make verify` (`scripts/verify.sh`) runs: ruff check + ruff format --check, pytest (`-n auto`), syntax checks for `start.py`/`stop.sh`/`cluster_assets/*.sh`/`trainer_image/*.sh`, eslint, `tsc --noEmit && vite build`, and `scripts/i18n_check.py` (en / zh-CN key parity; `--strict` also scans for hardcoded strings).

Single checks: `cd backend && uv run pytest tests/test_runs.py -q`, `cd frontend && npm run typecheck`.

## Layout

```
backend/app/
  main.py            create_app(start_background=...)
  core/              config (pydantic-settings, YAML + TUNINGPAD_* env), db (SQLAlchemy/SQLite),
                     auth, errors (AppError envelope), aws/k8s client factories
  jobs/engine.py     staged pipeline engine + reconcilers
  pipelines/         stage definitions per job type: setup, cluster, agent, dataset, trainer, run, serving
  services/          domain logic used by routers and pipelines
  routers/           FastAPI routes (thin)
  render/train.py    verl Hydra overrides / RayJob rendering
  catalog/           model + P-family instance catalogs
  planner.py         FSDP/Megatron, full/LoRA, TP/CP/EP planning
  metrics/verl_log.py
backend/tests/       hermetic pytest suite (conftest.py)
frontend/src/        v2/ shell + ui kit, pages/, components/, lib/api.ts, locales/{en,zh-CN}/common.json
templates/           agent templates (template.yaml + agent/ overlay)
trainer_image/       trainer Dockerfile + CodeBuild buildspec
cluster_assets/      guardian.py (in-cluster CronJob), pinned AWS LBC IAM policy
config/              tuningpad.example.yaml (real tuningpad.yaml is gitignored)
data/, .run/         local runtime state — gitignored, never commit
```

## Conventions

Backend
- Python 3.12, ruff line length 100, rules `E F I W UP B`. Dependencies are pinned to exact versions (`==`); keep it that way. Manage with `uv`, not pip.
- Always create AWS/K8s clients via `app.core.aws.client(...)` and `app.core.k8s.*`, never `boto3.client` directly. Tests swap these factories.
- Tag every AWS resource TuningPad creates with `aws.tags()` / `aws.tag_map()`.
- Raise `AppError` / `NotFound` / `Conflict` (`app/core/errors.py`) with a stable dotted `code`. The console localizes `apiErrors.<code>`, so a new user-facing code needs a key in both locale files.
- Pipeline stages (`pipelines/*`, registered with `jobs.engine.register`) must be idempotent: inspect real AWS/K8s state before acting, so a restarted backend can re-run the first unfinished stage. Persist data for later stages in `ctx.context`.
- Keep routers thin and put logic in `services/`.

Tests
- `tests/conftest.py` isolates every test: temp data dir, SQLite, and AWS/K8s factories that raise on use. Stub clients explicitly (`StubClient`). Tests must never touch real AWS or Kubernetes.
- Add or adjust tests alongside backend changes.

Frontend
- React 18 + TypeScript + Vite, react-router, i18next. No UI framework; use the `v2/` kit (`ui.tsx`, `v2.css`) that follows the sample-agentcore-launchpad V2 style.
- All backend calls go through `lib/api.ts` (`request` → `ApiError`). Don't call `fetch` elsewhere.
- No hardcoded user-facing text: use `t()` and add every key to both `locales/en/common.json` and `locales/zh-CN/common.json`.

## Safety

- This tool provisions billable GPU capacity. Do not run anything against a real AWS account (start.py with real credentials, CloudFormation, HyperPod scale-up, CodeBuild) unless the user asks for it. Keep the cost guardrails intact: guardian CronJob, `activeDeadlineSeconds` on RayJobs, explicit cost confirmation before any GPU scale-up.
- Don't weaken network/auth boundaries: the loopback-only default without `TUNINGPAD_PASSWORD`, gateway (18765) and vLLM (8000) reachable only from the AgentCore SG, `require_registered_sessions`, and IRSA scoping.
- Never commit `config/tuningpad.yaml`, `data/` or `.run/`.

<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->
