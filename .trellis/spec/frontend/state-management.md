# State Management

## State owners

There is no Redux, Zustand, React Query, or form-state dependency in
`frontend/package.json`. Keep resource snapshots and drafts local, using the
existing hooks. Paths below are relative to `frontend/src/`.

| Category | Owner and source | Rule |
| --- | --- | --- |
| Server snapshots | `useLoad` in `v2/hooks.ts` | Local to each hook instance; reload explicitly |
| Job lifecycle | `useJob` in `lib/useJob.ts` | Poll snapshot; notify owner after a terminal transition |
| Form drafts | `RunWizard` in `pages/Runs.tsx` | `useState` for values, step, preview, busy, confirmation |
| Resource identity/subpage | `RunsPage`, `ClustersPage` | URL `view` and `id`; details also use `tab` |
| Auth gate | `AuthGate` in `auth/AuthGate.tsx` | Resolve session before rendering the console |
| Auth consumer state | `AuthContext` in `auth/auth-context.ts` | Only `authRequired` and `logout` |
| Transient feedback | `V2ToastProvider` in `v2/ui.tsx` | Shell-scoped success/error messages |
| Language | i18next instance in `i18n.ts` | `V2Lang` switches en/zh-CN; detector persists preference |
| Sidebar preference | `Shell` in `v2/Shell.tsx` | `tuningpad_nav_collapsed` localStorage map |

Do not add a global resource store for a new page. Existing contexts have narrow
purposes, not a general application-state contract. Form data, GPU selections,
and auth secrets do not belong in the sidebar/language preference storage.

## Server state and mutations

Use typed API objects from `lib/api.ts`; no `fetch` elsewhere. `useLoad` fetches
on mount/key changes and exposes `reload`. It is not a shared cache: duplicate
loaders do not share snapshots or invalidate one another.

After a mutation, explicitly refresh the affected owner or navigate to the new
resource. In `pages/Clusters.tsx`, `ClusterDetail` keeps a local `jobId` and uses
`jobId ?? cl.job?.id ?? null` for its active job. Group callbacks set that ID and
reload the cluster. `JobPanel.onDone` reloads it again after job completion.
`RunWizard` navigates to the returned run ID after `runApi.create` succeeds.
Do not optimistically mark AWS resources ready when only a job ID was returned.

Keep loading and error distinct from empty/success. `useLoad` retains previous
data during reload and failure; `RunWizard.defaultsReady` additionally checks
loading/error before allowing progress. Resource lists use `Table`'s loading
and error props; mutations commonly use `useV2Toast` plus `errorMessage`.

## Controlled drafts and derived values

`RunWizard` stores explicit `params` separately from `compute: RunCompute`,
selected IDs, and the backend preview. Its `body(): CreateRunBody` assembles the
wire payload at submission time. Follow that boundary instead of sending the
entire component state object.

Derive selected objects from snapshots and IDs, as `chosen`, `tpl`, `cluster`,
and `valDs` do in `RunWizard`. Its `runtimes` projection uses `useMemo`; small
lookups and readiness expressions are computed directly. Use functional
updates when the new object depends on the old one, as `applyPreset` does for
`setCompute`. Keep `busy` and confirmation state in the action-owning component.

URL state should remain navigable: `RunDetail`/`ClusterDetail` preserve
`{ view: "detail", id, tab }` when setting search parameters. Keep ephemeral
wizard steps and unsaved input local; a query change is not draft persistence.

## Training defaults: display is not an override

`pages/Runs.tsx`'s `RunWizard` and `NumField` encode a critical distinction:

1. `runApi.defaults()` in `lib/api.ts` returns backend parameter and agent-loop
   defaults. Do not maintain a second hardcoded training-default table in JSX.
2. `paramDefaults` combines those with the selected template's `agent_loop`.
   It is display-only; `params` contains explicit user/preset values.
3. `NumField` displays inherited numeric values as placeholders, not input
   values. Blank learning rate/memory utilization keep Auto; unset training
   steps use `params.runAllEpochs`.
4. `applyPreset` deliberately writes explicit overrides. Switching templates
   updates inherited displays without overwriting explicit values.
5. Clearing a numeric field assigns `undefined`; `body` omits `undefined` and
   empty-string values. Valid zeroes must survive; never filter by truthiness.

The existing payload assembly preserves this distinction:

```ts
params: Object.fromEntries(
  Object.entries(params).filter(([, v]) => v !== undefined && v !== ""),
),
```

A runtime with no template may use generic backend loop defaults. A runtime
whose template metadata is pending, failed, or missing **is not template-less**.
`needsTemplate`, `defaultsLoading`, `defaultsError`, and `defaultsReady` in
`RunWizard` enforce that distinction: show loading/error/retry and block the
model step until resolved. Do not copy stale display data into `params`.

## Shared state boundaries

`AuthGate` stores `loading`/`in`/`out`, password, error, and busy locally. It
listens for `AUTH_UNAUTHORIZED_EVENT` from `lib/api.ts`; a non-auth API 401
returns the UI to sign-in. Keep password state ephemeral and preserve the
API client's same-origin credentials handling.

`i18n.ts` owns language detection (localStorage, navigator, htmlTag) and updates
the document language on changes. Use `useTranslation` or `useLocalized`, not
a parallel React language store. `Shell` only persists sidebar collapse as a
best-effort browser convenience; `V2ToastProvider` removes messages after 3200ms.

## Common mistakes

- Initializing `params` from visible defaults freezes inherited behavior into
  explicit overrides. Preserve the display/override separation established by
  commit `a60bdeb` and documented in
  `.trellis/tasks/archive/2026-10/10-07-training-param-defaults/prd.md`.
- Replacing missing template metadata with generic defaults silently displays
  the wrong loop settings. The archived review caught this; maintain the
  readiness gate and retry path even if a previous snapshot exists.
- Using `value || fallback` for optional numbers discards valid zeroes;
  `NumField` checks null/undefined explicitly and uses empty string for clearing.
- Assuming a mutation updates all lists automatically: the loader has no global
  invalidation; use the owner's `reload` or navigate as existing flows do.
- Storing server readiness in a local boolean after submission conflates queued
  work with completion. Preserve the job/resource polling and cost-confirmation
  flow rather than inventing a client-only lifecycle.
