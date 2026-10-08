# Frontend Development Guidelines

## Scope

TuningPad's `frontend/` is a React 18 + TypeScript + Vite console for the
FastAPI control plane. It uses react-router-dom, i18next, lucide-react, and the
in-repository V2 UI kit ported from sample-agentcore-launchpad. There is no
external UI framework, general global store, or frontend test runner configured
in `frontend/package.json`. Extend the existing kit rather than adding new
dependencies. These English guidelines describe the checked-in implementation.

## Guidelines Index

| Guide | Description | Status |
| --- | --- | --- |
| [Index](./index.md) | Frontend scope, guide inventory, pre-development and quality checklists | Filled |
| [Directory Structure](./directory-structure.md) | Page/query routing, V2/domain boundaries, API utilities, CSS and asset placement | Filled |
| [Component Guidelines](./component-guidelines.md) | V2 composition and props, loading/error UI, controlled forms, confirmations and accessibility limits | Filled |
| [Hook Guidelines](./hook-guidelines.md) | useLoad dependency keys, polling cadence, job transitions, cleanup and race limitations | Filled |
| [State Management](./state-management.md) | Local drafts/server snapshots, URL/context ownership, inherited training defaults versus overrides | Filled |
| [Quality Guidelines](./quality-guidelines.md) | Actual lint/build/i18n gates, mock-only browser QA, safety and defaults regression checks | Filled |
| [Type Safety](./type-safety.md) | Strict compiler settings, handwritten API contracts, unknown/null handling and assertion limits | Filled |

## Pre-Development Checklist

- Read the applicable guide above and the real neighboring code before editing.
  `frontend/src/pages/Runs.tsx` and `pages/Clusters.tsx` demonstrate complete
  list/wizard/detail flows; `v2/ui.tsx` defines the reusable controls.
- For navigation changes, inspect `frontend/src/App.tsx` and `v2/nav.ts`'s
  `NAV`. Subpages use `view`, `id`, and `tab` query parameters.
- For IO, extend `frontend/src/lib/api.ts`'s typed endpoint objects and DTOs.
  Every backend call uses that module (`request`/`requestForm` → `ApiError`);
  never put `fetch` in another file.
- For state, inspect `v2/hooks.ts`'s `useLoad`, `lib/poll.ts`'s `usePoll`, and
  `lib/useJob.ts`'s `useJob`; none is a shared server cache.
- All authored UI text uses `t()` plus keys in both
  `frontend/src/locales/en/common.json` and `locales/zh-CN/common.json`.
  Error-code messages use `apiErrors.<code>`; do not guess key namespaces.
- Preserve explicit cost confirmation before GPU scale-up, plan-purchase fee
  confirmation, and destructive-action dialogs. Keep auth and same-origin
  request behavior intact; no real AWS/Kubernetes operations for routine QA.

## Quality Check

- Run focused `npm run lint` and `npm run typecheck` from `frontend/`.
- Run `python3 scripts/i18n_check.py --strict` from the repository root when
  changing copy; default parity alone does not catch missing usage keys.
- Run repository-root `make verify` before completion. `scripts/verify.sh`
  includes frontend ESLint, `npm run build` (TypeScript plus Vite), and i18n
  parity, as well as backend and shell/lifecycle checks.
- Check changed UI behavior with a loopback mock API and both locales; there
  is no installed frontend test framework. Use `TUNINGPAD_API` in
  `frontend/vite.config.ts` to avoid the default real-backend proxy target.
- For training fields, exercise inherited defaults, presets, explicit zeroes,
  clearing, template switching, missing metadata, and retry recovery. The
  archived `.trellis/tasks/archive/2026-10/10-07-training-param-defaults/prd.md`
  records the real bugs behind these checks.

## Common mistakes

- Treating copied kit CSS comments as product features: `AuthGate` implements
  sign-in, not the launchpad registration/workspace flows mentioned in styles.
- Assuming a successful build proves browser behavior or runtime DTO validation:
  the quality and type-safety guides state the actual limits.
- Documenting a desired framework instead of using the current kit/hooks, or
  removing cost/default-readiness guards to simplify a form. Verify symbols and
  behavior in source when updating these guidelines.
