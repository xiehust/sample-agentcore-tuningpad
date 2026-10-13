# TuningPad

TuningPad is a no-code web console for agentic reinforcement learning on AWS. It trains agents built on [agentcore-rl-toolkit](https://github.com/xiehust/agentcore-rl-toolkit) on SageMaker HyperPod (EKS), using verl GRPO. Rollouts run on Bedrock AgentCore Runtime. The same cluster is also used to export checkpoints, serve models with vLLM, and evaluate them. The UI follows the V2 style of [sample-agentcore-launchpad](../sample-agentcore-launchpad).

```
Console (React) ─► FastAPI control plane (this host) ─► CloudFormation · SageMaker HyperPod · EKS · AgentCore · S3 · ECR · CodeBuild
                                                          │
  HyperPod EKS cluster (VPC) ── RayJob (verl + rollout gateway :18765) ◄── AgentCore runtime (VPC mode, sg-acr)
                             ── Job (verl.model_merger) · vLLM Deployment + internal NLB · guardian CronJob
```

## Workflow

| Step | Page | Notes |
|---|---|---|
| 1 | Settings | Preflight checks identity, permissions, GPU quota and tools. Region setup creates the S3 bucket, IAM roles and ECR repositories. |
| 2 | Clusters | Create a cluster with the official HyperPod EKS quick-setup stack (VPC, EKS, FSx, HyperPod with a CPU `system` node), or import an existing one. TuningPad then installs its components: access entry, IRSA, SGs, KubeRay, AWS LBC, FSx PVC and the guardian. P-family GPU instance groups are added or scaled per run, on-demand, through a training plan, or on Spot. |
| 3 | Agents | Use a template (GSM8K Math, OfficeBench), upload a `.zip`, or point at an ECR image. TuningPad runs static contract checks, builds an arm64 image, probes `/ping` locally, pushes to ECR, deploys an AgentCore runtime (VPC mode in the cluster subnets; Runtime V2 snapshot platform where the region offers it, else V1) and smoke-tests it with Bedrock `gpt-oss-20b`. |
| 4 | Datasets | Built-in GSM8K (7473 / 200 val / 1319 test) or OfficeBench (tasks and testbeds uploaded to your bucket), or upload JSONL/CSV/parquet. Data is converted to a `payload` parquet. |
| 5 | Models | Checks gateway chat-template compatibility (Qwen3/3.5/3.6/Coder, Nemotron-3, GLM4-MoE, GPT-OSS). The resource planner picks FSDP or Megatron, full or LoRA, and TP/CP/EP. |
| 6 | Training runs | A wizard with template presets and a cost estimate. The trainer image is built automatically by CodeBuild. Training runs as a RayJob (multi-node uses EFA), with live metrics and logs, auto-retry from the latest checkpoint, stop/resume, and run budget and deadline limits. |
| 7 | Exports → Inference → Evaluations | Merge a checkpoint into HF safetensors on FSx/S3. Serve it with vLLM behind an internal NLB with an API key. Evaluate base vs. trained models through your agent. |
| – | Resources & cost | Shows S3, ECR, runtimes, clusters and the guardian ledger, and can purge temporary prefixes. |

Cost guardrails that keep running when the console is offline:
- The in-cluster guardian CronJob scales idle GPU groups to 0 and enforces the cluster budget.
- `activeDeadlineSeconds` on every RayJob.
- Every GPU scale-up requires an explicit cost confirmation.

## Run locally

Prerequisites:
- uv ≥ 0.12
- Node ≥ 20
- Docker with buildx (an arm64 host builds agent images natively)
- AWS credentials for the target account
- Git and network access to the pinned toolkit source/package index when the relevant caches are cold (no sibling checkout required)

```bash
make install
./start.py            # backend http://127.0.0.1:8100, console http://127.0.0.1:5180
./stop.sh
make verify           # ruff + pytest + eslint + tsc/vite build + i18n parity
```

Configuration goes in `config/tuningpad.yaml` (see `config/tuningpad.example.yaml`) or in `TUNINGPAD_*` environment variables. Without `TUNINGPAD_PASSWORD`, the console only serves loopback clients. Set a password before binding a non-loopback host.

`helm` and `kubectl` are downloaded on demand into `data/bin`, pinned to a version and verified by sha256.

## Toolkit source and caches

The single source declaration is `backend/pyproject.toml`: Git URL
`https://github.com/xiehust/agentcore-rl-toolkit.git` at full commit
`7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6` (toolkit 0.1.5).
`backend/uv.lock` records that pin; from `backend/`, `uv sync --locked` verifies the
locked installation. No sibling checkout is required or searched automatically;
installation is not editable.

There are two separate caches:
- **uv's installation/build cache** supplies the installed Python package and build
  dependencies. Installation may access GitHub/package indexes.
- **TuningPad's source cache**, `<data_dir>/cache/toolkit/<full_sha>/`, supplies tracked
  repository inputs for Agent wheels/templates, Trainer contexts and the OfficeBench
  helper. It is fetched only on first actual use; a valid cache is reused without
  fetching source. Imports, health, meta and the toolkit preflight check do not download
  it. A cold cache is a warning, not proof that the network is unavailable. Other
  project preflight checks retain their existing AWS/ledger behavior.

A warm source cache **does not guarantee an offline build**: `uv build` runs in a private
copy and may still need build dependencies (such as hatchling) from the network. Offline
operation requires the relevant uv caches as well. A cold-cache fetch failure is explicit;
there is no sibling fallback. Invalid caches are reported, not silently replaced.

`toolkit_path` is optional and unset by default. If explicitly set (including via
`TUNINGPAD_TOOLKIT_PATH`), it must identify a clean checkout root of the **same repository
and exact SHA**, with the required tracked inputs and safe internal links. Prefer an
absolute path; relative paths are interpreted from the backend process's working directory.
Staged/unstaged changes, non-ignored untracked files, a different revision, or an invalid
origin are rejected even if the default cache is valid. Ignored build residue is not
packaged. TuningPad never checks out, resets or cleans this user directory.
**Dirty development trees and editable installs no longer satisfy this contract.** An
existing local checkout need not be moved or deleted; correct or remove an obsolete
`toolkit_path` setting yourself. Real configuration is not migrated automatically.

This change does not update existing cloud images or AgentCore runtimes, nor prevent
existing runtimes from being used. Later user-requested operations may enter the existing
image build/deploy flow and incur its normal costs. New Trainer records/tags use the full
SHA rather than old short/dirty identifiers. This is not a recipe digest: changing a
Dockerfile alone still does not force a new tag, and pinning toolkit source does not pin
base images or all build/data dependencies.

## Layout

```
backend/app/        core (config, db, auth, aws/k8s client factories), jobs/engine.py (staged pipelines),
                    pipelines/ (setup, cluster, agent, dataset, trainer, run, serving), services/, render/train.py,
                    catalog/ (models, P-family instances), planner.py, metrics/verl_log.py, routers/
frontend/src/       V2 shell + ui kit (from launchpad), pages/, components/, lib/api.ts, locales/{en,zh-CN}
templates/          gsm8k_math, officebench (template.yaml + agent overlay)
trainer_image/      CUDA 13 + EFA + verl Dockerfile and CodeBuild buildspec
cluster_assets/     guardian.py, pinned AWS LBC IAM policy
```

## Security notes

- The rollout gateway (tcp/18765) and vLLM (tcp/8000) are reachable only from the AgentCore security group inside the cluster VPC. The gateway only accepts sessions the trainer registered (`require_registered_sessions`). Traffic is plain HTTP inside the VPC.
- Rollout results include the per-session gateway key. They live under `rollouts/` and `smoke/`, which have 14-day and 1-day S3 lifecycle rules.
- The platform principal gets EKS cluster-admin through an access entry. Pods use IRSA roles scoped to the platform bucket, AgentCore invoke, and (for the guardian) `UpdateCluster` on that one cluster.
