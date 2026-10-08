# Type Safety

## Compiler and lint contract

`frontend/tsconfig.json` enables `strict`, `noUnusedLocals`,
`noUnusedParameters`, `noFallthroughCasesInSwitch`, and
`noUncheckedSideEffectImports`. It targets ES2022 with bundler resolution,
`react-jsx`, JSON imports, and `noEmit`; both `src` and `vite.config.ts` are
included. Do not weaken these settings to make a feature compile.

`frontend/eslint.config.js` includes TypeScript ESLint's recommended rules,
including explicit-`any` checks. The current source uses `unknown` for open
payloads rather than explicit TypeScript `any`. HTML `step="any"` in numeric
inputs is unrelated. Run `cd frontend && npm run typecheck` while iterating;
`npm run build` also typechecks before Vite bundles.

## Type ownership

Paths below are relative to `frontend/src/`.

- `lib/api.ts` is the handwritten wire-contract source: `Job`, `Cluster`,
  `RunView`, `Template`, `CreateRunBody`, `RunCompute`, `GroupBody`, and
  `NodeGroupBody` are exported there. Extend those with the backend contract;
  do not create a divergent page-local version of the same response.
- Endpoint objects (`api`, `clusterApi`, `runApi`, `agentApi`, `servingApi`)
  specify response types through generic HTTP helpers. Larger request shapes
  have interfaces; short ones are inline object types on the API method.
- `v2/ui.tsx` owns presentation types (`Option`, `Column<T>`, `TagTone`),
  `v2/hooks.ts` owns `Loaded<T>`/`ToastFn`, and `v2/nav.ts` owns navigation types.
- Component-only props remain beside the function, as in `JobPanel` and
  `TrainingPlanField`; use `ReactNode` for compositional slots.
- Import types with `import type` or inline `type` specifiers, as `Runs.tsx`
  imports `CreateRunBody` and `RunCompute` alongside the API objects.

## Typed transport is not runtime schema validation

All backend calls stay in `lib/api.ts`. `request<T>` serializes `json`, sets
JSON headers, and uses same-origin credentials. Multipart uploads use
`requestForm<T>` without forcing a JSON Content-Type. Both call
`parseResponse<T>`; never add `fetch` in a component or hook.

`parseResponse` initially holds JSON as `unknown`, then asserts the successful
body as `T`. It does not check every DTO field. There is no Zod/Yup/io-ts
validator in `frontend/package.json`. A type argument is a compile-time
contract, not evidence that the server returned a valid shape.

| Boundary | Existing behavior in `lib/api.ts` |
| --- | --- |
| Non-OK response | `ApiError` with code, localized message, unknown detail, status |
| No code in error body | Fallback `http.<status>` code |
| Non-auth endpoint returns 401 | Dispatch `AUTH_UNAUTHORIZED_EVENT` for `AuthGate` |
| Successful response is not JSON | Throw `ApiError` with `http.invalid_json` |
| Error code has a locale key | `localizedMessage` uses `apiErrors.<code>` |
| Network `TypeError` | `errorMessage` uses `apiErrors.network` |

Keep catches compatible with `unknown` and call `errorMessage(err)` rather than
assuming an arbitrary thrown value has `.message`. Any new user-facing backend
error code needs entries in both `locales/en/common.json` and
`locales/zh-CN/common.json`; unknown server messages remain a fallback.

## Open payloads, nullable values, and inference

`Template.agent_loop`, run `params`, and cluster `params` are
`Record<string, unknown>` because their keys vary. Preserve that uncertainty;
narrow individual values before numeric operations. `NumField` in
`pages/Runs.tsx` checks `typeof fallback === "number"` before displaying a
numeric default, and distinguishes null/undefined from explicit zero.

`RunCompute.max_hours` and `budget_usd` are nullable. Optional fields such as
`provider` are different from required nullable fields. Preserve that distinction
when building `CreateRunBody`; `RunWizard.body` removes undefined/empty-string
parameter entries instead of fabricating values for them.

Use existing unions where the contract is finite: `JobStatus`,
`PlatformChoice`, `RunCompute["capacity"]`, `NodeGroupBody["capacity"]`.
Do not force all resource statuses into `JobStatus`: `Cluster.status` and
`RunView.status` are strings; `StatusTag` supports unknown statuses with a gray
fallback and raw label when no translation exists.

Reuse inference rather than casts for tables/loaders. `Table<T>` derives its
row shape from `rows`, while `useLoad` infers from the API return promise.
The chart shape is explicitly `[number, number][]` in `RunView.series` and
`components/LineChart.tsx`, not an unstructured nested numeric array.

## Assertions: current limits, not blanket permission

The source has narrow assertions; it does not have a no-casts policy:

- `GroupForm` and `RunWizard` cast `Select` strings to indexed capacity unions.
  These options are locally enumerated. Keep the options synchronized with the
  union; prefer typed `Segmented<T>` where its UI fits.
- `ModelsPage` casts a query parameter to a tab union. This does not validate
  a user-edited URL; check membership when malformed inputs matter.
- `Shell.readCollapsed` casts parsed localStorage to a boolean map and catches
  JSON syntax failures, not schema failures. Do not extend this to trusted
  resource/auth data without actual checks.
- `request<T>`/`parseResponse<T>` centralize transport assertions. Do not spread
  `as SomeResponse` across pages to hide mismatched backend/frontend fields.

## Common mistakes

- Replacing `unknown` with `any` makes variable template metadata look safe
  without checking it. Narrow at the consuming field, as `NumField` does.
- Treating a cast as validation: `as RunCompute["capacity"]` cannot make an
  arbitrary external value a supported capacity mode.
- Using truthiness for optional numbers drops zero, a case explicitly covered
  by the archived training-defaults task and `backend/tests/test_runs.py`.
- Inventing non-null values while metadata loads hides a distinct error state.
  `RunWizard` gates unresolved template defaults instead of asserting `tpl!`.
- Assuming TypeScript validates translation keys: `i18n.ts` loads plain JSON
  dictionaries without a typed-key augmentation. Run
  `python3 scripts/i18n_check.py --strict` from the repository root and use
  browser QA for dynamic keys.
