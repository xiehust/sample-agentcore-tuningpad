# Quality Guidelines

## Actual verification commands

The repository-wide completion gate is `make verify`, implemented by
`scripts/verify.sh`. Its frontend stages run ESLint, then
`tsc --noEmit && vite build`, then locale key parity. The same gate also runs backend ruff/pytest and
lifecycle/shell syntax checks; a frontend-only pass is not the full gate.

```bash
# Focused checks while editing (repository root)
(cd frontend && npm run lint)
(cd frontend && npm run typecheck)
(cd frontend && npm run build)
python3 scripts/i18n_check.py --strict
# Required final gate
make verify
```

`frontend/package.json` has no `test` script and no Jest, Vitest, Testing Library,
Playwright, or other frontend test-framework dependency. Do not claim unit/E2E
coverage from a successful build or add a dependency just to follow a generic
React testing template. Frontend behavior is checked through local browser QA;
backend contract changes get hermetic tests in `backend/tests/`.

## Lint and typecheck expectations

- `frontend/eslint.config.js` combines JavaScript/TypeScript recommended rules
  and react-hooks recommended rules. `react-refresh/only-export-components` is
  a warning with `allowConstantExport`; `dist` is ignored.
- `frontend/tsconfig.json` uses strict checking, unused-variable/parameter
  checks, switch fallthrough checks, and side-effect import checks. Keep those
  enabled. Vite bundling alone is not a substitute for `npm run typecheck`.
- Keep exceptions narrow and explained. `TrainingPlanField` documents its
  callback dependency exception; `RunLog` disables `no-control-regex` only for
  ANSI-log stripping. Neither justifies file-wide lint disables or `any`.
- No frontend formatter command is configured in `package.json`. Match the
  nearby TypeScript style rather than claiming a Prettier gate exists.

## Localization is a separate gate

All authored user-facing text must use `t()` with matching keys in
`frontend/src/locales/en/common.json` and `frontend/src/locales/zh-CN/common.json`.
This includes placeholders, dialog copy, tooltips, retry actions, and accessible
labels. `lib/api.ts` localizes stable error codes via `apiErrors.<code>`;
`lib/localized.ts`'s `useLocalized` handles bilingual template metadata.

`scripts/i18n_check.py` has two distinct modes:

- Default, used by `make verify`: compare namespace files and flattened en/zh-CN
  key sets. It does not establish that a referenced key exists in either file.
- `--strict`: also scan TS/TSX for prose-like JSX text/attributes and missing
  static `t()` keys. Unused keys are report-only. Technical-token/brand
  allowlists and dynamic prefixes make this a heuristic, not a full UI audit.

Run strict mode when changing copy, and check both languages in the browser.
`RunWizard` uses `v2.common.retry`, not a guessed common namespace. The archived
`.trellis/tasks/archive/2026-10/10-07-training-param-defaults/prd.md` records a
retry translation-key bug that browser QA found after other checks passed.

## Safe browser QA

Never point behavioral QA at a real AWS account. Even apparently read-only
pages may invoke backend discovery. Use a loopback mock API with representative
responses and failures; do not launch training, scale GPUs, purchase plans,
start CodeBuild, or run `start.py` with real credentials without authorization.

`frontend/vite.config.ts` proxies `/api` for both dev and preview to
`TUNINGPAD_API`, defaulting to `http://localhost:8100`. Override that upstream
to the mock server for QA. The 5180 console port is selected by project startup,
not hardcoded in this Vite config. Keep QA artifacts out of tracked source.

For affected flows, verify:

1. Initial loading, empty data, failure/retry, and refresh with old data visible;
   `ClusterList`, `Table`, `Spin`, and `Alert` are the reference states.
2. Query navigation (`view`, `id`, `tab`), reload/back behavior, and keyboard use
   of `Select`/dialogs. Check narrow layouts and both en/zh-CN labels.
3. Mutations submit the intended payload once; busy controls disable actions;
   job completion reloads the resource rather than asserting it is ready early.
4. GPU scale-up still requires explicit cost confirmation in `RunWizard`,
   `GroupForm`, and `NodeGroupForm`. Cancellation sends no scale-up request.
5. Cluster deletion still requires an exact name; plan purchase displays the
   upfront fee before `plansApi.buy` submits `confirm_upfront_fee`.

Kit `testId` props and native `data-testid` attributes provide stable selectors
(e.g. `tp-run-next`, `tp-run-param-*`, `v2-confirm-ok`); they do not imply an
installed automated browser test suite.

## Training-form regression checklist

The archived defaults task above and commit `a60bdeb` supply concrete cases:

- Fresh wizard: fixed defaults appear numerically; only genuine planner choices
  remain Auto; unset training steps describe running all epochs.
- GSM8K/OfficeBench switch: inherited agent-loop defaults change; explicit
  values and presets remain explicit. A template-less runtime uses fallback.
- Pending/failed/missing metadata: no invented generic template defaults;
  visible loading/error/retry and blocked progression until resolution.
- Clearing fields omits overrides from the request; valid zero remains zero.
- Inspect the actual submitted payload, not only rendered placeholders.

`backend/tests/test_runs.py` exercises defaults routing and renderer resolution;
that does not replace browser checks of display, editing, namespace resolution,
and request omission. The archived task used a mock-only loopback upstream.

## Common mistakes and review blockers

- Direct `fetch` outside `frontend/src/lib/api.ts` bypasses typed transport,
  localized `ApiError`, and auth expiry. Keep `request`/`requestForm` centralized.
- Adding a UI framework, chart library, or state dependency duplicates the V2
  kit and existing SVG `LineChart`. Use `v2/ui.tsx` and `app.css` instead.
- Equating default locale parity with correct UI text misses keys absent from
  both dictionaries. Strict scanning and bilingual browser checks are needed.
- Treating default placeholders as submitted values reintroduces the defaults
  bug; preserve `RunWizard`'s separate `paramDefaults` and `params` state.
- Removing confirmation to simplify a form, or automatically setting resource
  state to ready after a job submission, weakens cost/lifecycle safeguards.
- Declaring keyboard accessibility complete because a dialog has `role=dialog`:
  the kit has known focus/labeling limits documented in the component guide.
