# Hook Guidelines

## Existing hooks and ownership

TuningPad uses React hooks and a small in-repository loader/poller, not React
Query or SWR. All paths below are relative to `frontend/src/`.

| Hook | Source | Contract |
| --- | --- | --- |
| `useLoad<T>(fetcher, key)` | `v2/hooks.ts` | `Loaded<T>`: nullable data/error, loading, reload |
| `usePaged<T>(rows, pageSize = 10)` | `v2/hooks.ts` | Local slice, upper-bounded page, pages, setPage, total |
| `useV2Toast()` | `v2/hooks.ts` | Shell callback accepting success/error tone and text |
| `usePoll(fn, ms, active = true)` | `lib/poll.ts` | Interval only while active; no immediate invocation |
| `useJob(jobId, onDone?)` | `lib/useJob.ts` | Job, error, async reload; polls until terminal |
| `useLocalized()` | `lib/localized.ts` | Selects a template's en/zh-CN label pair |
| `useAuth()` | `auth/auth-context.ts` | Auth-required flag and async logout |

Keep shared hooks named `use*` in their existing layer. Private control hooks
`useListbox` and `useEscape` stay in `v2/ui.tsx`. Do not export a page's local
state to another page merely to reuse fetching behavior.

## Fetch with useLoad and typed API objects

`ClusterList` and `RunList` use `useLoad` for the initial request and `usePoll`
for refresh. No page or hook should introduce `fetch`; add endpoints in
`lib/api.ts` and call the appropriate API object.

```tsx
const list = useLoad(clusterApi.list, "clusters");
usePoll(list.reload, LIST_POLL_MS);
const detail = useLoad(() => clusterApi.get(id), id);
```

`useLoad` stores the latest fetcher in a ref and runs its effect only when
`key` or its reload nonce changes. Include every changing request input in
that key: `DatasetDetail` in `pages/Datasets.tsx` keys its preview by dataset ID,
split, and dataset status. A new inline fetcher alone does not trigger a load.

The key is an effect dependency, **not** a shared cache key. Two `useLoad`
instances with the same key still make independent requests. `reload()` returns
void and increments a nonce; it is not an awaitable completion signal.

The effect marks itself non-live during cleanup so superseded effects cannot
write their result. It keeps previous data while loading and on failure,
clears the prior error at reload start, and formats errors with `errorMessage`.
It does not abort the network request or validate returned JSON. An old data
snapshot may remain while a new key loads; consumers must gate actions using
loading/error/readiness, not just `data !== null`.

## Polling and job completion

`lib/poll.ts` defines `DETAIL_POLL_MS = 2500` and `LIST_POLL_MS = 8000`.
`usePoll` keeps the latest callback in a ref, creates an interval based on
`ms`/`active`, and clears it on cleanup. Pair it with an initial load; it does
not call immediately, pause on hidden tabs, or serialize async requests.

Use each page's established cadence rather than polling everything at 2.5s:

- `RunList` and `ClusterList`: list interval, 8s.
- `RunDetail` in `pages/Runs.tsx`: 10s for its `ACTIVE` statuses, 60s otherwise.
- `ClusterDetail` in `pages/Clusters.tsx`: 5s while live, 16s otherwise.
- `RunLog`: initial effect plus 10s polling only while live.
- `useJob`: immediate load; 2.5s polling while no snapshot exists or the job is
  in `LIVE_JOB` (`queued`, `running`).

`useJob` keeps `onDone` in a ref. It calls that callback on an observed
live-to-terminal transition, not when the first fetched snapshot is already
terminal. `JobPanel` passes this through so owners can reload their resource.
On a job-ID change it clears the prior status/job and loads again. Unlike
`useLoad`, its async load does not have a stale-response guard; do not assume
all hooks provide the same race protection.

## Custom effects and callbacks

- `components/TrainingPlanField.tsx` debounces `plansApi.check` by 400ms. It
  tracks a live flag, clears the timer, and ignores responses for old inputs.
  Preserve ARN, cluster, instance type, and count as validation dependencies.
- `components/JobPanel.tsx`'s `JobLog` uses refs for byte offset and the scroll
  container, resets on job-ID changes, and fetches bounded batches until EOF.
  `RunLog` has a different chunk guard/text cap; do not assume they are identical.
- `frontend/eslint.config.js` enables recommended react-hooks rules. Keep
  callbacks/effect dependencies explicit; do not silence dependency warnings
  to freeze a stale closure. `TrainingPlanField` has a narrowly commented
  `onValid` exception, not a blanket exemption for other effects.
- `main.tsx` uses `StrictMode`. Preserve cleanup for intervals and event
  listeners (`AuthGate`, `useListbox`), and keep billable mutations in explicit
  action handlers, never mount effects.

## Common mistakes

- A constant `useLoad` key with a changing region, ID, or split leaves the old
  request until reload. Use the request inputs in the key, as in `DatasetDetail`.
- Assuming a poll timer cancels or deduplicates requests: `usePoll` only invokes
  its callback. Slow async calls can overlap; design new polling work accordingly.
- Reimplementing terminal detection in a view instead of using `useJob` loses
  the existing transition callback contract. Conversely, treating `onDone` as
  an initial-terminal notification is incorrect.
- Unconditionally converting API failures into empty arrays hides important
  errors. `RunDetail` does this for optional pod data, but required defaults in
  `RunWizard` explicitly show error/retry and block progress. Follow the latter
  for metadata that determines submitted training behavior.
- Promoting `usePaged` to a server-pagination abstraction: it only slices an
  in-memory list and caps the exposed page at the number of pages. It does not
  enforce a lower bound or validate `pageSize`; `Pager` disables backward
  navigation at page 1. Existing resource lists mostly render their loaded rows directly.
