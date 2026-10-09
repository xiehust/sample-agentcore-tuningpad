"""Render a training run into Kubernetes objects.

Layers (later wins): FIXED (agentcore_sync contract) → strategy defaults
(FSDP / Megatron, from the toolkit's verified scripts) → planner output →
user parameters. Output: Hydra overrides for `python -m verl.trainer.main_ppo`,
the agent-loop YAML, the in-pod entry script, and the RayJob manifest.

Topology: every Ray node is a GPU node of the chosen instance group (head
included, so `trainer.nnodes == nodes`); multi-node adds EFA devices and
NCCL/libfabric env. The rollout gateway advertises the Ray node IP (= VPC pod
IP via the VPC CNI), which the VPC-mode AgentCore runtime reaches through the
cluster security group's tcp/18765 rule from sg-acr.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

import yaml

from ..catalog.instances import InstanceSpec

GATEWAY_PORT = 18765
# Must match the trainer image (toolkit uv.lock pins ray 2.56.0).
RAY_VERSION = "2.56.0"
SUBMITTER_IMAGE = f"rayproject/ray:{RAY_VERSION}-py312"
FSX_MOUNT = "/fsx"
CONFIG_MOUNT = "/tp"

# Fixed by the agentcore_sync trainer mode + PayloadDataset contract.
FIXED: dict[str, Any] = {
    "trainer.use_v1": True,
    "trainer.v1.trainer_mode": "agentcore_sync",
    "algorithm.adv_estimator": "grpo",
    "algorithm.norm_adv_by_std_in_grpo": True,
    "algorithm.use_kl_in_reward": False,
    "data.custom_cls.path": "pkg://agentcore_rl_toolkit.backends.verl.dataset",
    "data.custom_cls.name": "PayloadDataset",
    "actor_rollout_ref.actor.loss_agg_mode": "seq-mean-token-sum",
    "actor_rollout_ref.actor.use_kl_loss": True,
    "actor_rollout_ref.actor.kl_loss_type": "low_var_kl",
    "actor_rollout_ref.actor.entropy_coeff": 0,
    "actor_rollout_ref.rollout.name": "vllm",
    "actor_rollout_ref.rollout.mode": "async",
    "actor_rollout_ref.rollout.calculate_log_probs": True,
    "actor_rollout_ref.rollout.agent.num_workers": 1,
    "actor_rollout_ref.rollout.agent.default_agent_loop": "agentcore_agent",
    "actor_rollout_ref.rollout.agent.agent_loop_config_path": f"{CONFIG_MOUNT}/agentcore_agent.yaml",
    "actor_rollout_ref.model.use_remove_padding": True,
    "trainer.critic_warmup": 0,
    "trainer.resume_mode": "auto",
    "trainer.logger": '["console"]',
}

# User-facing parameter defaults (verified Qwen3.5-2B GSM8K run).
DEFAULT_PARAMS: dict[str, Any] = {
    "train_batch_size": 32,
    "ppo_mini_batch_size": None,  # None = train_batch_size (single update per step)
    "rollout_n": 8,
    "lr": None,  # None = planner
    "total_training_steps": None,
    "total_epochs": 1,
    "max_model_len": 4096,
    "max_prompt_length": 2048,
    "max_response_length": 4096,
    "max_tokens_per_turn": None,  # None = template agent_loop
    "kl_loss_coef": 0.001,
    "rollout_is": "token",
    "rollout_is_threshold": 2.0,
    "temperature": 1.0,
    "val_temperature": 0.6,
    "val_n": 1,
    "gpu_memory_utilization": None,  # None = planner
    "save_freq": 10,
    "test_freq": 10,
    "val_before_train": True,
    "max_actor_ckpt_to_keep": 3,
    "tps_limit": None,
    "max_rollout_time": None,
    "enable_thinking": None,
}

# Shared with the console; templates may override these agent-loop fallbacks.
AGENT_LOOP_DEFAULTS = {
    "max_tokens_per_turn": 1024,
    "tps_limit": 8,
    "max_rollout_time": 600,
}

PARAM_BOUNDS = {
    "train_batch_size": (1, 4096),
    "rollout_n": (1, 64),
    "total_training_steps": (1, 100000),
    "total_epochs": (1, 100),
    "max_model_len": (512, 1_048_576),
    "max_prompt_length": (64, 1_048_576),
    "max_response_length": (64, 1_048_576),
    "kl_loss_coef": (0, 10),
    "temperature": (0, 2),
    "val_temperature": (0, 2),
    "val_n": (1, 64),
    "save_freq": (-1, 100000),
    "test_freq": (-1, 100000),
    "lr": (1e-9, 1e-2),
    "gpu_memory_utilization": (0.1, 0.95),
    "max_actor_ckpt_to_keep": (1, 1000),
}


def merge_params(user: dict[str, Any] | None) -> dict[str, Any]:
    from ..core.errors import AppError

    out = dict(DEFAULT_PARAMS)
    for k, v in (user or {}).items():
        if k not in DEFAULT_PARAMS:
            raise AppError("run.unknown_param", f"unknown training parameter {k}")
        if v is None:
            continue
        if k in PARAM_BOUNDS:
            lo, hi = PARAM_BOUNDS[k]
            if not lo <= float(v) <= hi:
                raise AppError("run.param_out_of_range", f"{k}={v} outside [{lo}, {hi}]")
        out[k] = v
    if out["max_prompt_length"] > out["max_model_len"]:
        raise AppError("run.bad_lengths", "max_prompt_length must be ≤ max_model_len")
    return out


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v) if abs(v) >= 1e-3 or v == 0 else f"{v:.0e}"
    return str(v)


def overrides(
    *,
    plan: dict[str, Any],
    params: dict[str, Any],
    spec: InstanceSpec,
    nodes: int,
    model_path: str,
    train_file: str,
    val_file: str | None,
    ckpt_dir: str,
    project: str,
    experiment: str,
    agent_loop: dict[str, Any] | None = None,
) -> list[str]:
    p = params
    profile = plan["profile"]
    lora = plan["strategy"].endswith("lora")
    mini = p["ppo_mini_batch_size"] or p["train_batch_size"]
    o: dict[str, Any] = dict(FIXED)
    o.update(
        {
            "data.train_files": f"['{train_file}']",
            "data.val_files": f"['{val_file or train_file}']",
            "data.train_batch_size": p["train_batch_size"],
            "data.max_prompt_length": p["max_prompt_length"],
            "data.max_response_length": p["max_response_length"],
            "actor_rollout_ref.model.path": model_path,
            "actor_rollout_ref.actor.optim.lr": p["lr"] or plan["lr"],
            "actor_rollout_ref.actor.ppo_mini_batch_size": mini,
            "actor_rollout_ref.actor.kl_loss_coef": p["kl_loss_coef"],
            "algorithm.rollout_correction.rollout_is": p["rollout_is"],
            "algorithm.rollout_correction.rollout_is_threshold": p["rollout_is_threshold"],
            "actor_rollout_ref.rollout.prompt_length": p["max_prompt_length"],
            "actor_rollout_ref.rollout.response_length": p["max_response_length"],
            "actor_rollout_ref.rollout.max_model_len": p["max_model_len"],
            "actor_rollout_ref.rollout.temperature": p["temperature"],
            "actor_rollout_ref.rollout.tensor_model_parallel_size": plan["rollout_tp"],
            "actor_rollout_ref.rollout.gpu_memory_utilization": p["gpu_memory_utilization"]
            or plan["gpu_memory_utilization"],
            "actor_rollout_ref.rollout.n": p["rollout_n"],
            "actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu": 1,
            "actor_rollout_ref.rollout.val_kwargs.n": p["val_n"],
            "actor_rollout_ref.rollout.val_kwargs.temperature": p["val_temperature"],
            "actor_rollout_ref.rollout.val_kwargs.do_sample": True,
            "actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu": 1,
            "trainer.default_local_dir": ckpt_dir,
            "trainer.project_name": project,
            "trainer.experiment_name": experiment,
            "trainer.val_before_train": p["val_before_train"],
            "trainer.n_gpus_per_node": spec.gpus,
            "trainer.nnodes": nodes,
            "trainer.save_freq": p["save_freq"],
            "trainer.test_freq": p["test_freq"],
            "trainer.total_epochs": p["total_epochs"],
            "trainer.max_actor_ckpt_to_keep": p["max_actor_ckpt_to_keep"],
        }
    )
    if p["total_training_steps"]:
        o["trainer.total_training_steps"] = p["total_training_steps"]
    if p["enable_thinking"] is not None:
        o["+data.apply_chat_template_kwargs.enable_thinking"] = bool(p["enable_thinking"])
    tok_per_gpu = max(8192, p["max_model_len"])
    if profile == "fsdp":
        o.update(
            {
                "actor_rollout_ref.model.enable_gradient_checkpointing": True,
                "actor_rollout_ref.actor.use_dynamic_bsz": True,
                "actor_rollout_ref.actor.ppo_max_token_len_per_gpu": tok_per_gpu,
                "actor_rollout_ref.actor.fsdp_config.optimizer_offload": plan["optimizer_offload"],
                "actor_rollout_ref.actor.fsdp_config.param_offload": plan["param_offload"],
            }
        )
        if lora:
            o.update(
                {
                    "actor_rollout_ref.model.lora_rank": plan["lora_rank"],
                    "actor_rollout_ref.model.lora_alpha": plan["lora_alpha"],
                    # vLLM loads the base weights from disk; only adapters are synced.
                    # With the default dummy format verl 0.9 pushes the whole base model
                    # as CPU tensors, and any tensor over the IPC bucket (e.g. the
                    # embedding) fails in rebuild_ipc with an IndexError.
                    "actor_rollout_ref.rollout.load_format": "safetensors",
                }
            )
    else:  # megatron (officebench / migration recipes)
        cp = plan["cp"]
        per_gpu = max(8192, p["max_model_len"] // max(cp, 1))
        o.update(
            {
                "actor_rollout_ref.actor.use_dynamic_bsz": False,
                "actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu": 1,
                "actor_rollout_ref.actor.checkpoint.save_contents": '["model"]',
                "actor_rollout_ref.actor.megatron.pipeline_model_parallel_size": 1,
                "actor_rollout_ref.actor.megatron.tensor_model_parallel_size": plan["tp"],
                "actor_rollout_ref.actor.megatron.context_parallel_size": cp,
                "actor_rollout_ref.actor.megatron.expert_model_parallel_size": plan["ep"],
                "actor_rollout_ref.actor.megatron.sequence_parallel": plan["tp"] > 1,
                "actor_rollout_ref.actor.megatron.use_dist_checkpointing": False,
                "actor_rollout_ref.actor.megatron.use_mbridge": True,
                "actor_rollout_ref.actor.megatron.override_transformer_config.recompute_granularity": "full",
                "actor_rollout_ref.actor.megatron.override_transformer_config.recompute_method": "uniform",
                "actor_rollout_ref.actor.megatron.override_transformer_config.recompute_num_layers": 1,
                "++actor_rollout_ref.actor.megatron.override_transformer_config.gradient_accumulation_fusion": False,
                "actor_rollout_ref.actor.megatron.param_offload": plan["param_offload"],
                "actor_rollout_ref.actor.megatron.grad_offload": plan["grad_offload"],
                "actor_rollout_ref.actor.megatron.optimizer_offload": plan["optimizer_offload"],
                "actor_rollout_ref.rollout.log_prob_use_dynamic_bsz": False,
                "actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu": per_gpu,
                "actor_rollout_ref.rollout.max_num_batched_tokens": 16384,
                "actor_rollout_ref.ref.log_prob_use_dynamic_bsz": False,
                "actor_rollout_ref.ref.log_prob_max_token_len_per_gpu": per_gpu,
                "actor_rollout_ref.ref.megatron.param_offload": True,
                "actor_rollout_ref.ref.megatron.pipeline_model_parallel_size": 1,
                "actor_rollout_ref.ref.megatron.tensor_model_parallel_size": plan["tp"],
                "actor_rollout_ref.ref.megatron.context_parallel_size": cp,
                "actor_rollout_ref.ref.megatron.sequence_parallel": plan["tp"] > 1,
            }
        )
        if plan["optimizer_offload"]:
            o.update(
                {
                    "++actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_cpu_offload": True,
                    "++actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_offload_fraction": 1.0,
                    "++actor_rollout_ref.actor.optim.override_optimizer_config.overlap_cpu_optimizer_d2h_h2d": True,
                }
            )
        if lora:
            o.update(
                {
                    "actor_rollout_ref.model.lora.rank": plan["lora_rank"],
                    "actor_rollout_ref.model.lora.alpha": plan["lora_alpha"],
                    "actor_rollout_ref.model.lora.merge": True,
                }
            )
    return [f"{k}={_fmt(v)}" for k, v in o.items()]


def agent_loop_yaml(template_loop: dict[str, Any], params: dict[str, Any]) -> str:
    """AgentCoreAgentLoop kwargs (cannot be set via Hydra CLI; verl reads this file)."""
    loop: dict[str, Any] = {
        "name": "agentcore_agent",
        "_target_": "agentcore_rl_toolkit.backends.verl.agent_loop.AgentCoreAgentLoop",
        "agent_runtime_arn": "${oc.env:AGENT_RUNTIME_ARN}",
        "s3_bucket": "${oc.env:ACR_S3_BUCKET}",
        "exp_id": "${oc.env:EXP_ID}",
        **{
            k: params.get(k) or template_loop.get(k, default)
            for k, default in AGENT_LOOP_DEFAULTS.items()
        },
        "gateway_port": GATEWAY_PORT,
        # The port is reachable from the agents' VPC ENIs: only pre-registered sessions.
        "require_registered_sessions": True,
    }
    for k in (
        "history_mode",
        "linear_on_nonlinear",
        "reward_extra_info_defaults",
        "reward_thresholds",
    ):
        if template_loop.get(k) is not None:
            loop[k] = template_loop[k]
    return yaml.safe_dump([loop], sort_keys=False)


def train_script(
    *,
    run_id: str,
    bucket: str,
    region: str,
    model_id: str,
    model_dir: str,
    data: dict[str, str],
    hydra: list[str],
    profile: str,
    run_dir: str,
) -> str:
    """Entry script executed as the Ray job driver on the head pod."""
    s3_prefix = f"s3://{bucket}/runs/{run_id}"
    fetch = "\n".join(
        f"aws s3 cp --only-show-errors {shlex.quote(src)} {shlex.quote(dst)}"
        for dst, src in data.items()
    )
    args = " \\\n    ".join(shlex.quote(h) for h in hydra)
    if profile == "megatron":
        # verl's default config (ppo_trainer) has no actor.megatron node: every megatron
        # override fails with "Key 'megatron' is not in struct" (run-dad915e023).
        args = "--config-name ppo_megatron_trainer \\\n    " + args
    patch = (
        "bash /opt/toolkit/patches/apply-megatron-bridge-cp-clamp.sh || true\n"
        if profile == "megatron"
        else ""
    )
    return f"""#!/usr/bin/env bash
# Rendered by TuningPad for run {run_id}. Runs as the Ray job driver on the head pod.
set -euo pipefail
RUN_DIR={shlex.quote(run_dir)}
mkdir -p "$RUN_DIR/data" "$RUN_DIR/ckpt" "$RUN_DIR/logs"
LOG="$RUN_DIR/logs/train.log"
export HF_HOME={FSX_MOUNT}/hf
{patch}
# Durable progress for the console: log + checkpoint index to S3 every 30 s.
# A step counts only while its actor/ dir exists: verl's max_actor_ckpt_to_keep deletes
# global_step_N/actor of rotated-out steps but leaves the global_step_N dir behind.
sync_progress() {{
  aws s3 cp --only-show-errors "$LOG" {s3_prefix}/logs/train.log 2>/dev/null || true
  ls -1d "$RUN_DIR"/ckpt/global_step_*/actor 2>/dev/null | xargs -r -n1 dirname \\
    | xargs -r -n1 basename \\
    | python3 -c 'import sys,json; print(json.dumps({{"steps": [l.strip() for l in sys.stdin]}}))' \\
    > "$RUN_DIR/logs/ckpt_index.json" || true
  aws s3 cp --only-show-errors "$RUN_DIR/logs/ckpt_index.json" {s3_prefix}/ckpt_index.json 2>/dev/null || true
}}
( while true; do sync_progress; sleep 30; done ) &
SYNC_PID=$!
trap 'kill $SYNC_PID 2>/dev/null || true; sync_progress' EXIT

echo "[tuningpad] staging data" | tee -a "$LOG"
{fetch}
if [ ! -f {shlex.quote(model_dir)}/config.json ]; then
  echo "[tuningpad] downloading {model_id}" | tee -a "$LOG"
  hf download {shlex.quote(model_id)} --local-dir {shlex.quote(model_dir)} >>"$LOG" 2>&1
fi
echo "[tuningpad] starting verl main_ppo" | tee -a "$LOG"
python3 -m verl.trainer.main_ppo \\
    {args} 2>&1 | tee -a "$LOG"
"""


def pod_env(
    *, run_id: str, region: str, bucket: str, runtime_arn: str, multi_node: bool, spec: InstanceSpec
) -> list[dict[str, Any]]:
    env = {
        "AGENT_RUNTIME_ARN": runtime_arn,
        "ACR_S3_BUCKET": bucket,
        "EXP_ID": f"rollouts/{run_id}",
        "GATEWAY_PORT": str(GATEWAY_PORT),
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "VERL_USE_EXTERNAL_MODULES": "agentcore_rl_toolkit.backends.verl.trainer",
        "HYDRA_FULL_ERROR": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "RAY_ENABLE_UV_RUN_RUNTIME_ENV": "0",
        "HF_HOME": f"{FSX_MOUNT}/hf",
        "NCCL_DEBUG": "WARN",
        "TORCH_NCCL_ASYNC_ERROR_HANDLING": "1",
    }
    if multi_node and spec.efa:
        env.update(
            {
                "FI_PROVIDER": "efa",
                "FI_EFA_USE_DEVICE_RDMA": "1",
                "FI_EFA_FORK_SAFE": "1",
                "NCCL_SOCKET_IFNAME": "^docker,lo,veth",
                # INIT,NET only: the train log then records the chosen transport
                # ("NET/OFI Selected provider is efa"), the evidence that EFA is in use
                "NCCL_DEBUG": "INFO",
                "NCCL_DEBUG_SUBSYS": "INIT,NET",
            }
        )
    return [{"name": k, "value": v} for k, v in env.items()]


def rayjob(
    *,
    run_id: str,
    name: str,
    namespace: str,
    image: str,
    spec: InstanceSpec,
    nodes: int,
    group: str,
    env: list[dict[str, Any]],
    config_map: str,
    service_account: str,
    pvc: str,
    ray_version: str = RAY_VERSION,
    active_deadline_s: int | None = None,
    node_selector: dict[str, str] | None = None,
) -> dict[str, Any]:
    """`node_selector` picks the GPU pool (HyperPod group or EC2 node group); default is
    the HyperPod instance group `group`."""
    multi = nodes > 1
    selector = node_selector or {
        "sagemaker.amazonaws.com/instance-group-name": group,
        "sagemaker.amazonaws.com/node-health-status": "Schedulable",
    }
    gpu_res: dict[str, Any] = {
        "nvidia.com/gpu": spec.gpus,
        "cpu": str(max(4, spec.vcpus - 8)),
        "memory": f"{int(spec.mem_gib * 0.85)}Gi",
    }
    if multi and spec.efa:
        gpu_res["vpc.amazonaws.com/efa"] = spec.efa
    shm = f"{min(512, int(spec.mem_gib * 0.25))}Gi"
    env_with_ip = env + [
        {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}
    ]
    volumes = [
        {"name": "fsx", "persistentVolumeClaim": {"claimName": pvc}},
        {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": shm}},
        {"name": "tp", "configMap": {"name": config_map, "defaultMode": 0o755}},
    ]
    mounts = [
        {"name": "fsx", "mountPath": FSX_MOUNT},
        {"name": "dshm", "mountPath": "/dev/shm"},
        {"name": "tp", "mountPath": CONFIG_MOUNT},
    ]

    def pod(container_name: str, ports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "serviceAccountName": service_account,
            "nodeSelector": dict(selector),
            "tolerations": [{"operator": "Exists", "effect": "NoSchedule"}],
            "containers": [
                {
                    "name": container_name,
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "env": env_with_ip,
                    "resources": {"limits": gpu_res, "requests": gpu_res},
                    "volumeMounts": mounts,
                    **({"ports": ports} if ports else {}),
                }
            ],
            "volumes": volumes,
        }

    spec_body: dict[str, Any] = {
        "entrypoint": f"bash {CONFIG_MOUNT}/train.sh",
        "submissionMode": "K8sJobMode",
        "shutdownAfterJobFinishes": True,
        "ttlSecondsAfterFinished": 600,
        "backoffLimit": 0,
        "rayClusterSpec": {
            "rayVersion": ray_version,
            "headGroupSpec": {
                "rayStartParams": {"dashboard-host": "0.0.0.0", "num-gpus": str(spec.gpus)},
                "template": {
                    "spec": pod(
                        "ray-head",
                        [
                            {"containerPort": 6379, "name": "gcs"},
                            {"containerPort": 8265, "name": "dashboard"},
                            {"containerPort": 10001, "name": "client"},
                            {"containerPort": GATEWAY_PORT, "name": "rollout-gw"},
                        ],
                    )
                },
            },
            "workerGroupSpecs": []
            if not multi
            else [
                {
                    "groupName": "gpu",
                    "replicas": nodes - 1,
                    "minReplicas": nodes - 1,
                    "maxReplicas": nodes - 1,
                    "rayStartParams": {"num-gpus": str(spec.gpus)},
                    "template": {
                        "spec": pod(
                            "ray-worker", [{"containerPort": GATEWAY_PORT, "name": "rollout-gw"}]
                        )
                    },
                }
            ],
        },
        "submitterPodTemplate": {
            "spec": {
                "restartPolicy": "Never",
                "serviceAccountName": service_account,
                "nodeSelector": {"sagemaker.amazonaws.com/instance-group-name": "system"},
                # small image on the CPU system node; only runs `ray job submit` + log follow
                "containers": [
                    {
                        "name": "submitter",
                        "image": SUBMITTER_IMAGE,
                        "resources": {"requests": {"cpu": "200m", "memory": "512Mi"}},
                    }
                ],
            }
        },
    }
    if active_deadline_s:
        spec_body["activeDeadlineSeconds"] = active_deadline_s
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayJob",
        "metadata": {"name": name, "namespace": namespace, "labels": {"tuningpad.io/run": run_id}},
        "spec": spec_body,
    }


def config_map(name: str, namespace: str, run_id: str, files: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": {"tuningpad.io/run": run_id}},
        "data": files,
    }


def describe(hydra: list[str]) -> str:
    return json.dumps(hydra, indent=1)
