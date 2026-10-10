# Image-only AgentCore Updates

## 1. Scope / Trigger

Operational image replacement without permission to invoke a model, change network/auth configuration, or recreate a runtime. Keep this distinct from the normal deploy-and-smoke pipeline.

## 2. API Signatures

Use `aws.client("bedrock-agentcore-control", region)`:

- `get_agent_runtime(agentRuntimeId=...)` returns the version, artifact and configuration.
- `update_agent_runtime(agentRuntimeId=..., agentRuntimeArtifact=..., roleArn=..., clientToken=..., ...)` creates the next version; acceptance is not readiness.
- `get_agent_runtime_endpoint(agentRuntimeId=..., endpointName="DEFAULT")` returns `status`, `liveVersion` and `targetVersion`.
- `list_tags_for_resource(resourceArn=...)` permits before/after tag comparison.

Check the installed SDK shape, but do not assume every returned/input-model field is mutable for every runtime.

## 3. Contracts

- Pin runtime IDs, desired ECR digest, old image/version, and configuration/tag fingerprints before writes. Do not persist environment-variable values in tracked artifacts.
- Preserve role ARN, network, lifecycle, protocol, metadata, platform and existing environment. An image-only operation must not silently upgrade PUBLIC/V1 to VPC/V2, or copy eval-only OTEL settings onto a training runtime.
- `networkModeConfig.requireServiceS3Endpoint` can be returned as `false` even though sending it in `UpdateAgentRuntime` is forbidden for post-rollout runtimes. SDK documentation describes a gradual rollout starting in May 2026; the observed us-east-1 rejection used a 2026-06-11 cutoff. Do not treat that regional error date as a universal cutoff.
- For a verified post-rollout runtime, omit the forbidden echo from the request, but retain the field in before/after verification. Do not flip it or provision a gateway to bypass validation. The existing `backend/app/services/agents.py:network_config()` already constructs only mode/subnets/security groups.
- Persist `deploying` before the mutation. Use a stable `clientToken` tied to actual request contents and reconcile ambiguous outcomes before any later action.
- Keep historical smoke results separate when the image changes without a model invocation. The 2026-10-10 maintenance operation kept the old image/result under `last_smoke.previous` and marked current validation `ready_only`; it did not fabricate `ok`, `total` or rewards.

## 4. Validation and Error Matrix

| Observation | Required behavior |
| --- | --- |
| Target image/config already adopted | Do not issue another update; check readiness and routing |
| Runtime READY but DEFAULT routes to old version | Still pending, not successful adoption |
| Forbidden S3-endpoint field causes ValidationException | Stop, reconcile unchanged old state, correct the payload; do not replay it unchanged |
| Update timeout/ambiguous response | Keep ledger unresolved/deploying; no blind retry or ready claim |
| Definite rejection, old runtime and DEFAULT verified unchanged | Restore only old ledger status, not the new image |
| Failed state or configuration/tag drift | Stop and report; no delete/recreate fallback under image-only consent |
| Read throttling | Bounded backoff honoring Retry-After; deterministic access/validation failures are not transient |

`backend/app/pipelines/agent.py:deploy_and_wait()` can delete/recreate a failed VPC runtime for supported-AZ recovery. Do not invoke that fallback when the authorized operation forbids deletion.

## 5. Good / Base / Bad Cases

- Good: new image/version is READY, DEFAULT is READY on the same version, fingerprints match, and the artifact digest is the approved one.
- Base: a resumed operation finds the desired version already ready and records verification without making a new version.
- Bad: HTTP success from update, an old smoke badge, or runtime READY alone is treated as proof of a new model rollout passing.

## 6. Required Verification

Before unattended execution, exercise old/new/failed states, configuration drift, stale DEFAULT routing, an ambiguous update timeout, deterministic rejection, and read throttling. Assert exactly one submitted mutation in a timeout scenario (no replay), a non-ready ledger until reconciliation, preserved old smoke evidence, and no new update request for an already-adopted image.

For approved live work, compare image/version/configuration/tags and DEFAULT `liveVersion` after READY. Canonicalize only semantically unordered fields, such as subnet/security-group lists; do not sort arbitrary arrays. READY/routing checks do not validate inference, reward computation or training.

## 7. Wrong vs Correct

Wrong: echo the entire GET network object because the SDK accepts its keys, or mark ready when `update_agent_runtime` returns.

Correct for these verified post-rollout runtimes (after a deep copy of the approved configuration):

```python
vpc = request["networkConfiguration"]["networkModeConfig"]
vpc.pop("requireServiceS3Endpoint", None)  # omit a forbidden echo, not a state change
# The original flag remains in the read-back fingerprint comparison.
```

Do not apply that omission blindly to legacy pre-rollout runtimes: follow their documented gateway semantics. Preserve the runtime and DEFAULT routing evidence independently of any later model smoke result.
