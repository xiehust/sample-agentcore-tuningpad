# Component Guidelines

## Build on the V2 kit

Use `frontend/src/v2/ui.tsx` and `v2/v2.css`, the launchpad-style V2 kit,
not an external UI framework or a new dependency. Product-specific reusable
components live in `frontend/src/components/`; route orchestration stays in
`frontend/src/pages/`. Paths below are relative to `frontend/src/`.

- List pages compose `PageHeader`, `Card flush`, `Table`, and `Empty`; see
  `ClusterList` in `pages/Clusters.tsx`.
- Creation flows compose `FlowHeader`, `Steps`, `Card`, `Field`, and `Select`;
  see `RunWizard` in `pages/Runs.tsx`. `Steps` disables forward jumps.
- Detail pages use `FlowHeader`, `SubTabs`, `Descriptions`, and `Kpi`;
  `RunDetail` also composes `JobPanel`, `StatusTag`, and `LineChart`.
- Keep private subcomponents near their page (`NumField`, `GroupForm`). Reuse
  `components/TrainingPlanField.tsx` for plan selection/validation rather than
  duplicating the ARN checker in every form.

## Props and composition contracts

Use function components with explicit object props, as in `JobPanel` and
`TrainingPlanField`. Import domain types from `lib/api.ts` with `type` imports.
Kit slots such as `Card.title`, `Card.end`, and `Confirm.body` accept `ReactNode`;
`PageHeader.title` and `Confirm.title` are strings. Check the actual signature.

- `Button` accepts `kind="primary" | "soft" | "danger"`, optional `size="sm"`,
  `disabled`, and `testId`; it defaults to `type="button"`, not submit.
- `Select` takes string `value`, `Option[]`, and `onChange(value: string)`.
  Its `placeholder` inserts a selectable empty-string option; it is not only
  visual hint text. `FilterSelect` is the labeled list-filter variant.
- `Table<T>` takes `Column<T>[]`, `rows`, and `rowKey(row): string`; column
  `render` functions return nodes. Pass a stable resource ID/name as `rowKey`.
- Domain callbacks communicate results upward: `NodeGroupsCard.onJob` receives
  the new job ID; `TrainingPlanField.onValid` receives `TrainingPlan | null`.
- `testId` on kit components maps to `data-testid`; native controls use
  `data-testid` directly. Existing product IDs start with `tp-`; kit IDs use `v2-`.

## Loading, errors, and actions

`v2/hooks.ts` supplies `useLoad`. Connect its states to the kit rather than
inventing a second spinner or error banner. `ClusterList` demonstrates:

```tsx
<Table
  rows={list.data ?? []}
  rowKey={(c) => c.id}
  columns={columns}
  loading={list.loading && !list.data}
  error={list.error}
  onRetry={list.reload}
/>
```

Here `columns` denotes the page's typed column definitions. `Table` shows a
spinner for an initial empty load, an error/retry row on failure, and an empty
state for no results. Detail pages such as `ClusterDetail` use `Spin` until
data exists and `Alert tone="error"` on an initial failure.

Call `useV2Toast` at component scope to obtain `toast`. Mutation handlers in
`RunWizard`/`GroupForm` set `busy`, call the typed API, show
`toast("error", errorMessage(err))` on failure, and clear `busy` in `finally`.
Disable the action while pending; reload or navigate on success. All backend
IO stays in `lib/api.ts`, never `fetch` inside a component.

## Forms and safety confirmations

Use controlled inputs with `v2-input`, wrapped in `Field` for label/hint/error
layout. `Field.required` is a visual asterisk, not HTML validation. Keep explicit
input values separate from inherited defaults; see `NumField` and the
[state management guide](./state-management.md).

Preserve `Confirm` for billable/destructive actions and `Modal` for typed-name
confirmation. These are safety behavior, not optional visual decoration:

- `RunWizard` previews plan/cost, opens `Confirm`, then submits through
  `runApi.create` with `confirm_cost: true`.
- `GroupForm` in `pages/Clusters.tsx` and `NodeGroupForm` in
  `components/NodeGroupsCard.tsx` require cost confirmation for positive counts;
  zero-count scale-down takes the direct submit path. Keep GPU scale-up gated.
- `PlansCard` in `pages/Clusters.tsx` confirms the upfront fee before
  `plansApi.buy`; `ClusterDetail` requires the exact cluster name for deletion.
- `Confirm.busy` disables its confirm button; do not assume it also disables
  cancel, backdrop dismissal, or Escape. Guard actions in the owning component.

## Styling, copy, and accessibility

- Use `v2-*` controls and `--v2-*` tokens from `v2/v2.css`; add product layout
  rules as `.v2 .tp-*` in `app.css` (`tp-stack`, `tp-row`, `tp-grid`, `tp-mono`).
  Small layout overrides via `style` already exist; do not introduce CSS-in-JS,
  Tailwind, or CSS modules as a competing styling system.
- Use lucide-react icons, as in `v2/ui.tsx` and `v2/Shell.tsx`. Decorative icons
  use `aria-hidden`; icon-only actions need a translated accessible name.
- All authored user-facing copy uses `useTranslation().t()` with keys in both
  `locales/en/common.json` and `locales/zh-CN/common.json`. Template label pairs
  use `useLocalized` from `lib/localized.ts`; numbers/dates use `lib/format.ts`.
- Preserve `Select`'s keyboard/listbox behavior (`useListbox`), `Alert`'s
  alert/status roles, `Spin`'s status role, and toast `aria-live` announcements.
- `Modal`/`Drawer` implement dialog roles, Escape, and backdrop close, but no
  focus trap or automatic title association. `Field` does not associate its
  sibling input via `htmlFor`. Check accessible names and keyboard focus when
  changing these surfaces; kit reuse alone is not proof of full accessibility.

## Common mistakes

- Replacing `Select` with a hand-rolled popup loses disabled-option navigation,
  search, focus return, and Escape isolation from its containing dialog.
- Moving a V2 dropdown outside the `.v2` root loses its theme tokens;
  `useListbox` intentionally renders beside the trigger rather than in a portal.
- Treating a red button as cost consent bypasses the explicit `Confirm` step.
- Using a guessed retry key: `RunWizard` uses `t("v2.common.retry")`. The archived
  `.trellis/tasks/archive/2026-10/10-07-training-param-defaults/prd.md` records
  a retry-button namespace bug caught by browser QA. Verify the full key in
  both locale files, including for hints, errors, and accessibility labels.
