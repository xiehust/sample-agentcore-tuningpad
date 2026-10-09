"""Export (checkpoint merge), inference (vLLM Deployment + internal NLB) and the
k8s manifests behind them. Evaluation itself runs from the backend through
RolloutClient → AgentCore runtime (VPC) → this endpoint."""

from __future__ import annotations

import secrets
from typing import Any

from ..catalog import instances
from ..render.train import FSX_MOUNT
from . import kube

VLLM_IMAGE = "vllm/vllm-openai:v0.24.0"  # same vLLM as the trainer stack (toolkit uv.lock)
VLLM_PORT = 8000


def export_dir(export_id: str) -> str:
    return f"{FSX_MOUNT}/exports/{export_id}"


def merge_job(
    *,
    export_id: str,
    run_id: str,
    step: int,
    backend: str,
    image: str,
    bucket: str,
    group: str,
    region: str,
    mem_gib: int,
    node_selector: dict[str, str] | None = None,
) -> dict[str, Any]:
    src = f"{FSX_MOUNT}/runs/{run_id}/ckpt/global_step_{step}/actor"
    dst = export_dir(export_id)
    script = (
        "set -euo pipefail\n"
        f"test -d {src} || {{ echo 'checkpoint {src} not found on FSx'; exit 2; }}\n"
        f"python -m verl.model_merger merge --backend {backend} --local_dir {src} "
        f"--target_dir {dst}\n"
        f"aws s3 sync --only-show-errors {dst} s3://{bucket}/exports/{export_id}/\n"
        f"du -sb {dst} | cut -f1 > {dst}/.size && echo merged $(cat {dst}/.size) bytes\n"
    )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"export-{export_id}".lower(),
            "namespace": kube.NAMESPACE,
            "labels": {"tuningpad.io/export": export_id},
        },
        "spec": {
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "tuningpad-workload",
                    "nodeSelector": node_selector
                    or {"sagemaker.amazonaws.com/instance-group-name": group},
                    "tolerations": [{"operator": "Exists", "effect": "NoSchedule"}],
                    "containers": [
                        {
                            "name": "merge",
                            "image": image,
                            "command": ["bash", "-c", script],
                            "env": [
                                {"name": "AWS_REGION", "value": region},
                                {"name": "AWS_DEFAULT_REGION", "value": region},
                                # the merge is CPU-only; the CUDA base image sets
                                # NVIDIA_VISIBLE_DEVICES=all, which makes the NVIDIA runtime
                                # fail the container on a GPU-less node (system group)
                                {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"},
                            ],
                            "resources": {"requests": {"cpu": "2", "memory": f"{mem_gib}Gi"}},
                            "volumeMounts": [{"name": "fsx", "mountPath": FSX_MOUNT}],
                        }
                    ],
                    "volumes": [{"name": "fsx", "persistentVolumeClaim": {"claimName": "fsx"}}],
                }
            },
        },
    }


def merge_resources(params_b: float | None, gpu_group: str) -> tuple[str, int]:
    """Small models merge on the CPU system node; big ones need GPU-node RAM."""
    need = int((params_b or 8) * 4) + 6  # fp32 consolidation of the shards in host RAM
    if need <= 26:  # system node: ml.m5.2xlarge, 32 GiB
        return "system", need
    return gpu_group, min(need, 1500)


def api_key() -> str:
    return "tp-" + secrets.token_urlsafe(24)


def endpoint_name(endpoint_id: str) -> str:
    return f"vllm-{endpoint_id}".lower()


def vllm_manifests(
    *,
    endpoint_id: str,
    model: str,
    served_name: str,
    group: str,
    instance_type: str,
    tp: int,
    replicas: int,
    tool_parser: str | None,
    reasoning_parser: str | None,
    sg_nlb: str,
    subnets: list[str],
    max_model_len: int | None,
    node_selector: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    spec = instances.spec(instance_type, serving=True)
    if tp > spec.gpus:
        from ..core.errors import AppError

        raise AppError("inference.tp_too_large", f"TP {tp} > {spec.gpus} GPUs per node")
    # /dev/shm is RAM: keep it a quarter of a small node (g5.2xlarge has 32 GiB in total)
    shm_gib = max(1, min(32, spec.mem_gib // 4))
    name = endpoint_name(endpoint_id)
    args = [
        "--model",
        model,
        "--served-model-name",
        served_name,
        "--port",
        str(VLLM_PORT),
        "--tensor-parallel-size",
        str(tp),
        "--api-key",
        "$(VLLM_API_KEY)",
        "--gpu-memory-utilization",
        "0.9",
    ]
    if max_model_len:
        args += ["--max-model-len", str(max_model_len)]
    if tool_parser:
        args += ["--enable-auto-tool-choice", "--tool-call-parser", tool_parser]
    if reasoning_parser:
        args += ["--reasoning-parser", reasoning_parser]
    labels = {"app": name, "tuningpad.io/endpoint": endpoint_id}
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": kube.NAMESPACE, "labels": labels},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": "tuningpad-workload",
                    "nodeSelector": node_selector
                    or {"sagemaker.amazonaws.com/instance-group-name": group},
                    "tolerations": [{"operator": "Exists", "effect": "NoSchedule"}],
                    "containers": [
                        {
                            "name": "vllm",
                            "image": VLLM_IMAGE,
                            "args": args,
                            "env": [
                                {
                                    "name": "VLLM_API_KEY",
                                    "valueFrom": {"secretKeyRef": {"name": name, "key": "api-key"}},
                                },
                                {"name": "HF_HOME", "value": f"{FSX_MOUNT}/hf"},
                            ],
                            "ports": [{"containerPort": VLLM_PORT, "name": "http"}],
                            "resources": {
                                "limits": {"nvidia.com/gpu": tp},
                                "requests": {"nvidia.com/gpu": tp},
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": VLLM_PORT},
                                "periodSeconds": 10,
                                "failureThreshold": 180,
                            },
                            "volumeMounts": [
                                {"name": "fsx", "mountPath": FSX_MOUNT},
                                {"name": "dshm", "mountPath": "/dev/shm"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "fsx", "persistentVolumeClaim": {"claimName": "fsx"}},
                        {
                            "name": "dshm",
                            "emptyDir": {"medium": "Memory", "sizeLimit": f"{shm_gib}Gi"},
                        },
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": kube.NAMESPACE,
            "labels": labels,
            "annotations": {
                "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                "service.beta.kubernetes.io/aws-load-balancer-scheme": "internal",
                "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
                "service.beta.kubernetes.io/aws-load-balancer-security-groups": sg_nlb,
                "service.beta.kubernetes.io/aws-load-balancer-manage-backend-security-group-rules": "false",
                "service.beta.kubernetes.io/aws-load-balancer-subnets": ",".join(subnets),
            },
        },
        "spec": {
            "type": "LoadBalancer",
            "selector": {"app": name},
            "ports": [{"name": "http", "port": VLLM_PORT, "targetPort": VLLM_PORT}],
        },
    }
    return [deployment, service]


SAMPLING_KEYS = ("max_tokens", "temperature", "top_p", "top_k")


def eval_sampling(
    template_loop: dict[str, Any] | None,
    run_params: dict[str, Any] | None,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-turn sampling for an eval, matching the training run's validation rollouts:
    `max_tokens` = the agent loop's max_tokens_per_turn, `temperature` = val_temperature.
    Without it every agent turn may generate up to max_model_len (vLLM's default)."""
    from ..render.train import merge_params

    p = merge_params(run_params or {})
    out: dict[str, Any] = {
        "max_tokens": int(
            p.get("max_tokens_per_turn") or (template_loop or {}).get("max_tokens_per_turn", 1024)
        ),
        "temperature": float(p["val_temperature"]),
    }
    for k, v in (override or {}).items():
        if k in SAMPLING_KEYS and v is not None:
            out[k] = v
    return out


def secret_manifest(endpoint_id: str, key: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": endpoint_name(endpoint_id), "namespace": kube.NAMESPACE},
        "type": "Opaque",
        "stringData": {"api-key": key},
    }


def read_api_key(region: str, eks: str, endpoint_id: str) -> str | None:
    import base64

    s = kube.get(region, eks, "v1", "Secret", endpoint_name(endpoint_id))
    if not s:
        return None
    data = (s.get("data") or {}).get("api-key")
    return base64.b64decode(data).decode() if data else None


def endpoint_state(region: str, eks: str, endpoint_id: str) -> dict[str, Any]:
    name = endpoint_name(endpoint_id)
    dep = kube.get(region, eks, "apps/v1", "Deployment", name) or {}
    svc = kube.get(region, eks, "v1", "Service", name) or {}
    ready = (dep.get("status") or {}).get("readyReplicas") or 0
    want = (dep.get("spec") or {}).get("replicas") or 0
    ingress = ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    host = ingress[0].get("hostname") if ingress else None
    return {
        "ready": ready,
        "replicas": want,
        "host": host,
        "url": f"http://{host}:{VLLM_PORT}/v1" if host else None,
    }


def summarize_eval(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-sample states (`evals.sample_state`, shared with the sample list so
    the counts always agree). Truncation is scored 0; both failure kinds count as 0 in the
    overall mean."""
    from .evals import reward_of, sample_state

    rewards, failed, truncated = [], 0, 0
    for it in items:
        state = sample_state(it)
        if state == "truncated":
            truncated += 1
            rewards.append(0.0)
        elif state == "scored":
            rewards.append(reward_of(it.get("result")) or 0.0)
        else:
            failed += 1
    n = len(items)
    return {
        "n": n,
        "scored": len(rewards),
        "failed": failed,
        "truncated": truncated,
        "mean_reward": round(sum(rewards) / n, 4) if n else None,  # failures count as 0
        "mean_reward_scored": round(sum(rewards) / len(rewards), 4) if rewards else None,
        "acr_failed_rate": round(failed / n, 4) if n else None,
    }
