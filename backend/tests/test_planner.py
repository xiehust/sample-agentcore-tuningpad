import pytest

from app.catalog.instances import CATALOG
from app.planner import plan


@pytest.mark.parametrize(
    "params_b,itype,nodes,ctx,moe,exp_strategy,exp_tp,exp_rtp",
    [
        # verified: Qwen3.5-2B FSDP full on 1×H100 and 8×H100
        (2.27, "p5.4xlarge", 1, 4096, False, "fsdp_full", 1, 1),
        (2.27, "p5.48xlarge", 1, 4096, False, "fsdp_full", 1, 1),
        # Qwen3-4B on 8×A100-40G: full state 8 GiB/GPU fits
        (4.0, "p4d.24xlarge", 1, 8192, False, "fsdp_full", 1, 1),
        # 8B on a single H100: 16 B/param = 119 GiB > 40 → LoRA
        (8.0, "p5.4xlarge", 1, 4096, False, "fsdp_lora", 1, 1),
        # verified: Qwen3.6-27B Megatron LoRA TP4 on 8×H100, 128k ctx → CP2
        (27.8, "p5.48xlarge", 1, 131072, False, "megatron_lora", 4, 4),
        # verified: Qwen3-Coder-30B-A3B MoE → Megatron LoRA TP4
        (30.5, "p5.48xlarge", 2, 32768, True, "megatron_lora", 4, 4),
        # long context alone routes a small model to Megatron
        (4.0, "p5en.48xlarge", 1, 131072, False, "megatron_lora", 1, 1),
    ],
)
def test_plan_table(params_b, itype, nodes, ctx, moe, exp_strategy, exp_tp, exp_rtp):
    p = plan(params_b=params_b, spec=CATALOG[itype], nodes=nodes, max_model_len=ctx, is_moe=moe)
    assert p.strategy == exp_strategy
    assert p.tp == exp_tp
    assert p.rollout_tp == exp_rtp
    assert p.world_size == CATALOG[itype].gpus * nodes


def test_qwen36_megatron_matches_verified_recipe():
    p = plan(params_b=27.8, spec=CATALOG["p5.48xlarge"], max_model_len=131072)
    assert (p.cp, p.lora_rank, p.lora_alpha, p.lr) == (2, 64, 128, 1e-5)
    assert p.gpu_memory_utilization == 0.7
    assert p.optimizer_offload and p.grad_offload and not p.param_offload


def test_moe_gets_expert_parallel():
    p = plan(params_b=30.5, spec=CATALOG["p5.48xlarge"], max_model_len=32768, is_moe=True)
    assert p.ep == 2  # 8 GPUs / TP4


def test_overrides_and_warnings():
    p = plan(params_b=8.0, spec=CATALOG["p5.4xlarge"], tuning="full")
    assert p.strategy == "fsdp_full"
    assert any("full fine-tune needs" in w for w in p.warnings)
    p = plan(params_b=2.0, spec=CATALOG["p5.4xlarge"], nodes=2)
    assert any("multi-node" in w for w in p.warnings)
    p = plan(params_b=2.0, spec=CATALOG["p5.48xlarge"], prefer="megatron")
    assert p.profile == "megatron"


def test_bigger_gpus_keep_full_finetune():
    # 8B full on 8×B200 (179 GiB): 16 B/param / 8 = 15 GiB/GPU
    p = plan(params_b=8.0, spec=CATALOG["p6-b200.48xlarge"])
    assert p.strategy == "fsdp_full"
    assert p.lr == 5e-6


def test_invalid_nodes():
    with pytest.raises(ValueError):
        plan(params_b=1, spec=CATALOG["p5.4xlarge"], nodes=0)
