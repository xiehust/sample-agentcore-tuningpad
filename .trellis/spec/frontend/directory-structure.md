# Directory Structure

## Scope and entry points

TuningPad's console is one React 18 + TypeScript application in `frontend/`.
`frontend/package.json` uses Vite, react-router-dom, i18next, and lucide-react;
there is no external UI framework, global-store package, or frontend test runner.
Use the existing V2 kit rather than adding a framework or dependencies.
Paths below are relative to `frontend/` unless stated otherwise.

## Directory layout

```text
frontend/
  public/favicon.svg       Static TuningPad mark
  src/main.tsx             CSS/i18n setup, StrictMode, AuthGate, App
  src/App.tsx              Lazy pages and BrowserRouter routes
  src/auth/                AuthGate.tsx and auth-context.ts
  src/v2/                  Shared V2 UI kit, shell, navigation, hooks, styles
  src/components/          Reusable TuningPad domain components
  src/pages/               Route-level lists, creation flows, and details
  src/lib/                 API client, polling, formatting, locale/region helpers
  src/locales/{en,zh-CN}/   common.json translation dictionaries
  src/i18n.ts              i18next initialization and language detection
  src/app.css              TuningPad-specific additions to V2 styles
```

`vite.config.ts`, `tsconfig.json`, and `eslint.config.js` live at the frontend
root. Root-repository `scripts/verify.sh` and `scripts/i18n_check.py` own the
quality gate; there is no `frontend/src/tests/` convention to follow.

## Page and navigation ownership

- `src/App.tsx` exports `App`; its `page` helper wraps lazy imports of default
  page exports. Add a top-level page there, under the `Shell` route.
- Routes are `/`, `/clusters`, `/agents`, `/datasets`, `/models`, `/runs`,
  `/exports`, `/inference`, `/evals`, `/resources`, `/settings`, and the `*` fallback.
- `src/v2/nav.ts` exports `NAV`, `NavItem`, and `NavGroup`. Add sidebar entries
  here with a translation `labelKey` and a lucide `icon`; do not hardcode them
  in `src/v2/Shell.tsx`. `Shell` owns active links, sidebar collapse, and the
  `Suspense` boundary around the route `Outlet`.
- Subpages use query parameters, not additional nested resource routes:
  `RunsPage` in `src/pages/Runs.tsx` chooses list/new/detail with `view` and `id`;
  `ClustersPage` in `src/pages/Clusters.tsx` also supports `view=import`.
  Details use `tab`; preserve `view` and `id` when switching tabs.

## Where new code belongs

| Responsibility | Existing home and example |
| --- | --- |
| Resource orchestration and local forms | `src/pages/Runs.tsx`: `RunList`, `RunWizard`, `RunDetail` |
| Reusable resource UI | `src/components/JobPanel.tsx`: `JobPanel`, `StageBar`, `JobLog` |
| Model/plan summaries | `src/components/ModelCards.tsx`: `ModelCheckCard`, `PlanCard` |
| Shared controls and layout | `src/v2/ui.tsx`: `Button`, `Select`, `Table`, `Card`, `Confirm` |
| Shell-wide presentation state | `src/v2/hooks.ts`: `ToastContext`, `useV2Toast` |
| Backend DTOs and calls | `src/lib/api.ts`: `Cluster`, `CreateRunBody`, `clusterApi`, `runApi` |
| Reusable stateful IO | `src/lib/useJob.ts`: `useJob`; `src/lib/poll.ts`: `usePoll` |
| Display helpers | `src/lib/format.ts`: `money`, `dateTime`, `duration`, `num` |
| Localized template metadata | `src/lib/localized.ts`: `useLocalized` |

Keep page-only helpers beside their callers: `NumField` and `groupName` are
local to `Runs.tsx`, while `TrainingPlanField` is shared by runs and clusters.
All backend calls belong in `src/lib/api.ts`; neither pages nor components
should contain `fetch`. Import the relevant typed API object instead.

## Names, styling, and assets

- Component/page files use PascalCase `.tsx` names; page exports such as
  `RunsPage` are default exports for lazy loading. Shared components use named
  exports such as `StatusTag` in `src/components/StatusTag.tsx`.
- Helpers use descriptive `.ts` filenames (`api.ts`, `format.ts`, `poll.ts`);
  custom hooks start with `use` even when grouped in `v2/hooks.ts`.
- Use direct relative imports, as in `Runs.tsx`; `tsconfig.json` defines no
  path aliases. Import shared wire types from `lib/api.ts`, not another page.
- `src/main.tsx` imports `v2/v2.css`, `v2/auth.css`, then `app.css`.
  V2 controls use `v2-*` classes and `--v2-*` tokens; product additions use
  `.v2 .tp-*` rules in `app.css`. Keep this layering rather than a second theme.
- `src/v2/Logo.tsx` exports the inline-SVG `V2Logo`; `public/favicon.svg` is
  the matching static artwork. General icons come from lucide-react.
- All new user-facing copy uses `t()` and matching keys in both
  `src/locales/en/common.json` and `src/locales/zh-CN/common.json`.

## Common mistakes

- Adding `/runs/:id` without considering existing `?view=detail&id=...` links:
  `App` and `RunsPage` implement query-based subpages today.
- Putting AWS URLs or another HTTP client inside a page: this bypasses
  `request`/`requestForm`, `ApiError`, and auth-expiry handling in `lib/api.ts`.
- Editing only `NAV` or only `App`: sidebar destinations and registered lazy
  pages must agree, and navigation labels need both locale entries.
- Copying the kit into a feature directory or installing a UI/chart framework:
  reuse `v2/ui.tsx` and `components/LineChart.tsx`, which already renders SVG.
- Treating copied style comments as product routes: `v2/auth.css` contains
  launchpad-era register/workspace styles, but `AuthGate` implements sign-in,
  not registration or a workspace store.
