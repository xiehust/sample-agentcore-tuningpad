# Durable cluster deletion

## 1. Scope / trigger

AgentCore VPC network interfaces can outlive runtime deletion. A slow service-side
release must not turn a confirmed cluster deletion into a failed task requiring repeated
manual retries. The job engine persists deferred checks, and CloudFormation starts
releasing compute before the network cleanup barrier.

This changes deletion orchestration for existing and new stacks; it does not change
their templates, directly delete nested stacks, or forcibly detach managed ENIs.

## 2. Signatures

- `StageContext.defer(*, delay_s, reason, detail=None, detail_params=None)` raises
  `JobDeferred` and releases the worker after the engine commits its waiting state.
- `Job.next_attempt_at` is a nullable UTC datetime, added by the existing additive
  schema upgrade. Old rows read as `None`.
- `JobEngine.resume_due()` claims due waiting jobs atomically. `start_scheduler()`
  runs these short ledger checks separately from resource reconcilers.
- `DELETE /api/clusters/{cid}?confirm={name}` calls
  `services.cluster_deletion.request_delete(cluster_id, confirm)`.

## 3. Contracts

- Job and stage status `waiting` is active, alongside `queued` and `running`. Job
  responses expose `next_attempt_at`; a waiting stage exposes `reason`, `detail_params`,
  `detail`, and `next_attempt_at`. Send UTC offsets, including for SQLite datetimes.
- Waiting does not set `ended_at`, emit a finish hook, or mark the cluster deleted.
  The frontend keeps polling, offers cancellation, and localizes structured wait reasons.
- On restart, preserve future check times, resume due waits, and recover interrupted
  queued/running jobs. Merge registered stages by name for historical jobs while
  retaining completed stages and `context`.
- Deletion order: workload cleanup, node groups, runtime delete requests, root-stack
  deletion/compute cleanup, network release, final root-stack deletion.
- Submit Service and namespace deletion together to stop workload controllers from
  recreating Services. Keep the load balancer controller and its nodes alive until
  Service finalizers have completed; an empty pre-deletion list alone is insufficient.
- Persist runtime deletion acknowledgements before proceeding. Repeating an explicit
  delete request reuses an active job or retries the original failed/cancelled job with
  its context. Failed/cancelled jobs are not automatically revived on startup.
- Use CloudFormation resource identities and its normal reverse dependency graph.
  Query owned subnets for `agentic_ai` interfaces, include managed interfaces, and
  consume paginated responses. Expected network dependency failures can be deferred;
  retain the actual stack identity through retries.
- Eight hours is a delayed-cleanup warning, not a terminal failure. Network cleanup
  continues automatically. Never promise a fixed AWS completion time.

## 4. Validation & error matrix

| Condition | Behavior |
| --- | --- |
| Confirmed delete repeated while queued/running/waiting | Return the original job ID |
| Wrong confirmation, even with an active delete | `cluster.confirm_name`; no mutation |
| Expected AgentCore ENIs or SG dependency remains | Persist waiting and next check |
| Known network-only CloudFormation deletion failure | Wait for dependencies and retry |
| Permissions denied, unexpected resource failure, unclassified failure | Surface failure; do not treat as absent |
| AgentCore reports `DELETE_FAILED` after accepting deletion | Fail with diagnostics; explicit retry can resubmit just the failed runtime, even if the earlier runtime stage succeeded |
| Root stack still deleting | Observe; do not repeatedly issue `DeleteStack` |
| User cancels waiting | Terminal cancellation; scheduler cannot revive it |
| Backend shuts down | Preserve resumable work; do not cancel the deletion intent |
| Imported cluster | Preserve unregister semantics; never delete its root stack |

## 5. Good / base / bad cases

- **Good:** compute is released, three ENIs remain beyond eight hours, the console
  explains the delay, and a later scheduled check completes cleanup without a click.
- **Base:** no network dependencies remain; normal CloudFormation deletion completes.
- **Bad:** a nested FSx/IAM/HyperPod deletion fails and is misclassified as an ENI delay,
  or a managed interface is hidden by a default EC2 query and cleanup is marked complete.

## 6. Tests required

- Engine: one-worker pool remains available after deferral; due time survives restart;
  concurrent checks claim one attempt; future/cancelled waits never execute early.
- Schema: an old jobs table gains nullable `next_attempt_at` without losing rows.
- Cleanup: root deletion is submitted while ENIs still exist; long waits remain active;
  eventual release completes; paginated/managed ENIs and nested failures are inspected;
  authorization errors propagate; imported resources are not deleted.
- Controller races: namespace termination prevents Service recreation and a later
  read verifies finalizers completed before root deletion. Runtime `DELETE_FAILED`
  must surface and remain recoverable through explicit retry from the network stage.
- API: repeated/concurrent requests do not create competing deletion jobs; explicit
  retry preserves runtime deletion context and completed stages.
- UI: English and Chinese waiting/delayed messages, next check, cancel action, and
  waiting-to-success polling transition using a mock backend.

## 7. Wrong vs correct

**Wrong:** sleep in a worker for eight hours, then fail because service-owned ENIs remain;
start a new delete job on every click; swallow every `DELETE_FAILED` or `AccessDenied`.

**Correct:** inspect real resource state, persist a deferred check for known dependencies,
release the worker, and resume the same durable intent. Classify CloudFormation failures
by current resource type and diagnostic evidence; treat unknown failures as real errors.
