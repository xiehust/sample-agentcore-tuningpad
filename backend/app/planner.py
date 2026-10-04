"""Resource planner: model × instance × nodes × context → training strategy.

Pure function, no I/O. The heuristics are deliberately simple and conservative
and anchored on configs the toolkit repo actually ran:

* Qwen3.5-2B   FSDP full, 1×H100 / 8×H100 (TP=2), gpu_mem_util 0.40, lr 5e-6
* Qwen3-4B     FSDP full / FSDP LoRA (8×A100-40G, lr 2e-5) / Megatron LoRA
* Qwen3.6-27B  Megatron LoRA r64, TP4 CP2, 8×H100, 128k ctx, offload, util 0.70, lr 1e-5
* Qwen3-Coder-30B-A3B  Megatron LoRA TP4 (MoE), 1-2 nodes

Memory model (bytes per parameter, per GPU after sharding):
  full fine-tune  16 B/param (bf16 weights+grads, fp32 master+Adam) / world_size
  LoRA            2 B/param (frozen bf16 weights) / shard factor
Activations and the colocated vLLM engine take the rest; we budget ~50% of HBM
for training state. The estimate is a warning, not a gate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from .catalog.instances import InstanceSpec

Strategy = Literal["fsdp_full", "fsdp_lora", "megatron_lora", "megatron_full"]

LONG_CONTEXT = 65536
MEGATRON_PARAMS_B = 20.0


@dataclass
class Plan:
    strategy: Strategy
    profile: Literal["fsdp", "megatron"]
    lora_rank: int | None
    lora_alpha: int | None
    lr: float
    tp: int  # Megatron tensor parallel (1 for FSDP)
    cp: int  # context parallel
    ep: int  # expert parallel (MoE)
    rollout_tp: int
    gpu_memory_utilization: float
    param_offload: bool
    optimizer_offload: bool
    grad_offload: bool
    world_size: int
    train_state_gib_per_gpu: float
    gpu_mem_gib: float
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _pow2_at_least(x: float, cap: int) -> int:
    n = 1
    while n < x and n < cap:
        n *= 2
    return n


def plan(
    *,
    params_b: float,
    spec: InstanceSpec,
    nodes: int = 1,
    max_model_len: int = 4096,
    is_moe: bool = False,
    prefer: Literal["auto", "fsdp", "megatron"] = "auto",
    tuning: Literal["auto", "full", "lora"] = "auto",
) -> Plan:
    if nodes < 1:
        raise ValueError("nodes must be >= 1")
    gpus = spec.gpus
    world = gpus * nodes
    mem = spec.gpu_mem_gib
    p = params_b * 1e9
    gib = 1024**3
    warnings: list[str] = []
    reasons: list[str] = []

    if nodes > 1 and not spec.multi_node:
        warnings.append(f"{spec.type} has no multi-node EFA fabric; use a single node")

    full_state = 16 * p / world / gib
    lora_state = 2 * p / min(world, 8) / gib
    full_fits = full_state <= 0.5 * mem

    # --- backend choice ---
    if prefer == "fsdp":
        profile = "fsdp"
    elif prefer == "megatron":
        profile = "megatron"
    elif is_moe or params_b >= MEGATRON_PARAMS_B or max_model_len >= LONG_CONTEXT:
        profile = "megatron"
        reasons.append(
            "Megatron: "
            + ", ".join(
                r
                for r, on in [
                    ("MoE model", is_moe),
                    (f"≥{MEGATRON_PARAMS_B:.0f}B params", params_b >= MEGATRON_PARAMS_B),
                    (f"context ≥{LONG_CONTEXT // 1024}k", max_model_len >= LONG_CONTEXT),
                ]
                if on
            )
        )
    else:
        profile = "fsdp"
        reasons.append("FSDP: dense model under 20B with short context")

    # --- full vs LoRA ---
    if tuning == "full":
        lora = False
    elif tuning == "lora":
        lora = True
    else:
        lora = not full_fits or profile == "megatron"
        reasons.append(
            "LoRA: full fine-tune state does not fit comfortably"
            if not full_fits
            else ("LoRA: matches the verified Megatron recipes" if lora else "full fine-tune fits")
        )
    if not lora and not full_fits:
        warnings.append(
            f"full fine-tune needs ~{full_state:.0f} GiB/GPU of training state "
            f"(HBM {mem:.0f} GiB); enable offload, add nodes, or use LoRA"
        )

    strategy: Strategy = f"{profile}_{'lora' if lora else 'full'}"  # type: ignore[assignment]

    # --- parallelism ---
    weights_gib = 2 * p / gib
    rollout_tp = _pow2_at_least(weights_gib / (0.35 * mem), gpus)
    tp = cp = ep = 1
    if profile == "megatron":
        tp = _pow2_at_least(weights_gib / (0.30 * mem), gpus)
        if params_b >= MEGATRON_PARAMS_B:
            tp = max(tp, min(4, gpus))  # verified: 27B / 30B-A3B ran TP4
        cp = 2 if max_model_len >= LONG_CONTEXT and gpus // tp >= 2 else 1
        if is_moe:
            ep = max(1, min(gpus // tp, 8))
        rollout_tp = max(rollout_tp, tp)

    # --- memory knobs ---
    state = lora_state if lora else full_state
    big = profile == "megatron" and params_b >= MEGATRON_PARAMS_B
    optimizer_offload = (not lora and full_state > 0.5 * mem) or big
    grad_offload = big
    param_offload = not lora and full_state > 0.9 * mem
    util = 0.7 if profile == "megatron" else (0.4 if params_b <= 8 else 0.5)

    if lora:
        lr = 1e-5 if profile == "megatron" else 2e-5
    else:
        lr = 5e-6 if params_b <= 8 else 2e-6

    if tp > gpus:
        warnings.append(f"tensor parallel {tp} exceeds GPUs per node ({gpus})")
    if spec.gpus == 1 and profile == "megatron":
        warnings.append("Megatron on a single GPU is not supported by the verified recipes")

    return Plan(
        strategy=strategy,
        profile=profile,
        lora_rank=(64 if profile == "megatron" else 32) if lora else None,
        lora_alpha=(128 if profile == "megatron" else 64) if lora else None,
        lr=lr,
        tp=tp,
        cp=cp,
        ep=ep,
        rollout_tp=rollout_tp,
        gpu_memory_utilization=util,
        param_offload=param_offload,
        optimizer_offload=optimizer_offload,
        grad_offload=grad_offload,
        world_size=world,
        train_state_gib_per_gpu=round(state, 1),
        gpu_mem_gib=mem,
        warnings=warnings,
        reasons=reasons,
    )
