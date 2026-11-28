# 0001. One AgentCore session is the rollout sandbox; only shared-image Tasks

Date: 2026-12-01
Status: Accepted

## Context

Multi-harness RL (FineEnvs, "The ultimate guide to multi-harness RL") runs unmodified **Harnesses** such as Claude Code inside a per-rollout sandbox, and takes **Tasks** from Harbor datasets. Harbor gives every Task its own container image and runs it on a sandbox backend (Docker, E2B, Daytona, Modal).

On AWS, an AgentCore runtime ARN is bound to one container image, and each AgentCore session is an isolated microVM. Docker-in-Docker does not work inside it (agentcore-rl-toolkit `designs/sandbox_dynamic_environments.md`). The rollout gateway that records tokens is reachable only from the AgentCore security group inside the cluster VPC.

The alternatives were:

1. A Harbor sandbox backend (E2B, Daytona). This adds a third-party service and credentials, and the sandbox must reach the VPC-only gateway from outside the VPC.
2. One AgentCore runtime per Task image (toolkit `examples/sandbox_harbor`). This needs thousands of runtimes per dataset, runs into runtime quotas, and needs arm64 rebuilds of every image. The example is evaluation-only and uses PUBLIC networking.
3. One loader runtime that unpacks each Task image with proot. The toolkit design for this has not been started.

## Decision

A rollout runs in one AgentCore session of the **Agent**. The Harness and the Task's files share that microVM. The Verifier is staged and run only after the Harness exits, and the session is discarded at the end of the rollout.

TuningPad accepts only Task sets whose Tasks share one environment image, such as SmolDataEnvs. TuningPad rebuilds that environment for arm64 into the Agent image and delivers each Task's files through the payload from S3. We do not use Harbor sandbox backends or per-Task runtimes.

## Consequences

- No new sandbox service, credentials or network path. The Harness reaches the gateway over the existing VPC path, and `require_registered_sessions` still applies.
- Datasets where each Task has its own image (SWE-bench, Terminal-Bench) are out of scope until option 2 or 3 is revisited.
- Rollouts do not depend on public internet: Harness versions, the environment's packages and the Verifier's dependencies are fixed at Agent build time.
