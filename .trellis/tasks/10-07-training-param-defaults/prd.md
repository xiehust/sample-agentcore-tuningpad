# 训练参数显示实际默认值

## Goal

Make the new-training-run form show its effective defaults instead of labeling every unset numeric parameter “自动” / “Auto”. Users should distinguish concrete defaults from model/resource-dependent planning without changing training behavior.

User request: “可以记成一个任务。然后把‘自动’有实际取值的改成直接显示默认值。随模型和资源变化的‘自动’才保留自动”.

## Background and Evidence

- `frontend/src/pages/Runs.tsx:94-109`: all numeric fields currently share the Auto placeholder. `Runs.tsx:152-165`: presets fill explicit parameters; unset values are omitted from the request.
- `backend/app/render/train.py:58-83`: authoritative fixed defaults. `train.py:105-121`: unspecified/null values retain backend defaults. `train.py:159-170`: learning rate and vLLM memory utilization fall back to the planner. `train.py:189-190`: no explicit training-step limit is emitted when unset.
- `backend/app/planner.py:65-185`: backend, tuning strategy, learning rate, and memory utilization depend on model/resource inputs.
- `backend/app/render/train.py:264-276`: agent-loop parameters inherit template values, with generic fallbacks. `templates/gsm8k_math/template.yaml:31-34` and `templates/officebench/template.yaml:16-19` specify current template defaults.
- `frontend/src/lib/api.ts:487-497`: template metadata already includes `agent_loop`. The existing run preview resolves general parameters only after prerequisites are selected; there is no standalone training-defaults endpoint today.

## Requirements

### R1 — Concrete defaults

Unset basic/advanced fields must visibly show the actual default, not Auto:

| Parameter | Default |
| --- | --- |
| train_batch_size | 32 |
| rollout_n | 8 |
| total_epochs | 1 |
| max_model_len | 4096 |
| max_prompt_length | 2048 |
| max_response_length | 4096 |
| kl_loss_coef | 0.001 |
| rollout_is_threshold | 2.0 |
| temperature | 1.0 |
| val_temperature | 0.6 |
| val_n | 1 |
| save_freq | 10 |
| test_freq | 10 |
| max_actor_ckpt_to_keep | 3 |

Displayed defaults must stay consistent with backend behavior rather than becoming an independently maintained set of training rules.

### R2 — Template-derived defaults

Show the selected Agent template's defaults for `max_tokens_per_turn`, `tps_limit`, and `max_rollout_time`. For GSM8K these are 1024, 8, and 180; for OfficeBench, 8192, 8, and 1800. When there is no template value, match the renderer's generic fallbacks of 1024, 8, and 600. Switching Agent/template updates inherited displays, without overwriting explicit user or preset values.

### R3 — Genuine automatic selection and step semantics

Keep Auto for unset learning rate and vLLM memory utilization, and preserve the Auto choices for training backend and full/LoRA strategy. For unset training steps, show “跑满轮数” / “Run all epochs” rather than Auto or a fabricated fixed step count.

### R4 — Editing, presets, and compatibility

Keep all numeric fields editable. Explicit user/preset values take precedence; clearing a field restores the applicable default display (or Auto/run-all-epochs behavior). Preserve existing parameter omission, backend resolution, preset application, and training behavior. Do not freeze inherited template defaults into explicit overrides. All added UI text must be localized in both English and Simplified Chinese.

## Acceptance Criteria

- [x] AC1 (R1): A fresh wizard displays every fixed default listed above; none of those unset fields says Auto.
- [x] AC2 (R2): GSM8K and OfficeBench display their own agent-loop values. Changing templates updates only inherited values; a no-template runtime uses the renderer fallbacks.
- [x] AC3 (R3): Only model/resource-dependent choices retain Auto. An unset training-step field describes running all epochs.
- [x] AC4 (R4): Presets and manual edits remain visible and effective. Clearing fields restores the appropriate inherited display; zero-valued valid inputs are not mistaken for unset values.
- [x] AC5 (R1–R4): Displayed defaults agree with backend resolution, without changing submitted explicit overrides or effective training configuration.
- [x] AC6 (R4): Both locales render correctly; focused regression checks and `make verify` pass. Verification must be local/hermetic and must not launch training or contact real AWS/Kubernetes.

## Out of Scope

- Changing any training default, planning heuristic, model strategy, or preset recipe.
- Changing inference/Agent creation forms, adding a frontend framework/test dependency, or redesigning the wizard.
- Provisioning resources, running GPU training, or modifying cost/auth/network guardrails.

## Implementation and Verification — 2026-10-07

The user approved the final plan with “ok”. Implementation and acceptance checks are complete; code remains uncommitted, so task archival/session auto-commits have not been run.

- Added read-only `GET /api/runs/defaults`, serving existing `DEFAULT_PARAMS` and shared `AGENT_LOOP_DEFAULTS`. The renderer uses the same loop fallback map; parameter values and precedence are unchanged.
- The wizard shows numeric defaults as placeholders without copying them into explicit overrides. Selected template metadata supplies loop defaults. Only learning rate, memory share, backend, and tuning retain Auto; unset training steps say Run all epochs / 跑满轮数.
- Missing, failed, or pending template metadata is distinguished from a genuinely template-less runtime. The form does not invent generic loop defaults for an unresolved template; it shows loading/error/retry and prevents proceeding until resolved.
- Added hermetic backend tests for defaults routing/resolution, current templates, explicit overrides, cleared values, and valid zero values.
- A read-only independent review found the unresolved-template fallback issue above; it was corrected and re-reviewed without blocking findings. Browser QA also caught and corrected the retry button's translation-key namespace.

Verification:

- `cd backend && uv run pytest tests/test_runs.py tests/test_planner.py -q`: PASS.
- `cd frontend && npm run typecheck && npm run lint`: PASS.
- Final `make verify`: PASS (ruff, pytest, lifecycle/shell syntax, eslint, TypeScript/build, locale parity). Existing dependency deprecation warnings remain; no dependencies changed.
- Isolated Chromium/agent-browser checks with a loopback in-memory mock API: PASS for fresh numeric defaults, Auto fields, both template values, template-less fallback, explicit values surviving template changes, zero values, clearing, preset application, request omission, English/Chinese, missing/pending/failed template metadata, failed defaults loading, and retry recovery. DOM events were used after native coordinate actions in this CLI build mis-targeted scrolled controls; browser state and rendered fields were asserted directly.
- Local-only QA artifacts: `.run/training-param-defaults-check.py` and `.run/training-param-defaults-zh.png` (gitignored). The frontend test instance used port 15180 and a mock-only upstream on 18199, never the running real backend.
- No real AWS/Kubernetes calls, resource provisioning, or training runs were performed.
