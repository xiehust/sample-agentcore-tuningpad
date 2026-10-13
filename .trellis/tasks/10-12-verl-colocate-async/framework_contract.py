"""Generate real control-plane renders, then compose against the fixed CPU SDK.
backend/.venv/bin/python SCRIPT generate WORK
WORK/toolkit/.venv/bin/python SCRIPT check WORK
No cloud clients, model loading, actors, GPU or training are started.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
WORK = Path(sys.argv[2]).resolve()


def generate():
    sys.path.insert(0, str(ROOT / "backend"))
    from app.catalog.instances import CATALOG
    from app.planner import plan
    from app.services.runs import render
    scenarios = []
    for mode in ("sync", "colocate_async"):
        for profile in ("fsdp", "megatron"):
            for tuning in ("full", "lora"):
                p = plan(params_b=2.27, spec=CATALOG["p5.48xlarge"], nodes=1,
                         max_model_len=4096, prefer=profile, tuning=tuning).to_dict()
                assert p["strategy"] == f"{profile}_{tuning}"
                run = {
                    "id": f"run-{mode}-{profile}-{tuning}", "model_id": "Qwen/Qwen3.5-2B",
                    "spec": {"training_mode": mode, "plan": p,
                             "params": {"train_batch_size": 8, "ppo_mini_batch_size": 4}},
                    "compute": {"instance_type": "p5.48xlarge", "nodes": 1,
                                "instance_group": "mock-gpu", "max_hours": 1 / 3600},
                }
                output = render(run, cluster={"region": "us-east-1"},
                                runtime_arn="arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/mock",
                                bucket="mock-bucket", image="mock:fixed", template_loop={},
                                datasets={"train": "s3://mock-bucket/train.parquet"}, attempt=0)
                assert output["rayjob"]["spec"]["activeDeadlineSeconds"] == 1
                scenarios.append({"mode": mode, "profile": profile, "tuning": tuning,
                                  "plan": p, "rendered": output})
    (WORK / "renders.json").write_text(json.dumps(scenarios, indent=2))
    print("GENERATED 8 actual service renders, deadline=1 second")


def check():
    from importlib.metadata import version
    from hydra import compose, initialize_config_module
    from hydra.errors import ConfigCompositionException
    from agentcore_rl_toolkit.backends.verl import trainer as module
    from verl.trainer.ppo.v1 import get_trainer_cls
    from verl.trainer.ppo.v1 import trainer_base
    from verl.trainer.ppo.utils import need_critic
    assert version("verl") == "0.9.0"
    assert version("transferqueue") == "0.1.8"
    print({k: version(k) for k in ("agentcore-rl-toolkit", "verl", "transferqueue", "torch", "hydra-core", "pytest")})
    results = []
    for row in json.loads((WORK / "renders.json").read_text()):
        mode, profile, tuning = row["mode"], row["profile"], row["tuning"]
        rendered = row["rendered"]
        config_name = "ppo_megatron_trainer" if profile == "megatron" else "ppo_trainer"
        with initialize_config_module(config_module="verl.trainer.config", version_base=None):
            cfg = compose(config_name=config_name, overrides=rendered["hydra"])
        assert cfg.trainer.v1.trainer_mode == f"agentcore_{mode}"
        assert cfg.trainer.use_v1 and cfg.algorithm.adv_estimator == "grpo"
        assert not need_critic(cfg) and not cfg.distillation.enabled
        assert cfg.actor_rollout_ref.actor.loss_agg_mode == "seq-mean-token-sum"
        assert cfg.actor_rollout_ref.rollout.mode == "async"
        assert cfg.actor_rollout_ref.rollout.calculate_log_probs
        assert cfg.algorithm.rollout_correction.rollout_is == "token"
        assert cfg.trainer.nnodes == 1 and cfg.trainer.n_gpus_per_node == 8
        assert cfg.actor_rollout_ref.actor.use_dynamic_bsz == (profile == "fsdp")
        if profile == "megatron":
            assert cfg.actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu == 1
            assert "--config-name ppo_megatron_trainer" in rendered["config_map"]["data"]["train.sh"]
        if tuning == "lora":
            if profile == "fsdp":
                assert cfg.actor_rollout_ref.model.lora_rank > 0
                assert cfg.actor_rollout_ref.rollout.load_format == "safetensors"
            else:
                assert cfg.actor_rollout_ref.model.lora.rank > 0
                assert cfg.actor_rollout_ref.model.lora.merge
        if mode == "colocate_async":
            assert cfg.trainer.v1.colocate_async.parameter_sync_step == 1
            assert cfg.trainer.v1.colocate_async.num_warmup_batches == 1
            assert cfg.trainer.v1.sampler.max_off_policy_threshold == 8
            assert cfg.trainer.v1.sampler.max_off_policy_strategy == "drop"
            assert "agentcore_colocate_async" not in cfg.trainer.v1
            # Negative control proves native + is required, not just a string test.
            without_plus = [s.removeprefix("+") if s.startswith("+trainer.v1.colocate_async.parameter_sync_step=") else s for s in rendered["hydra"]]
            try:
                with initialize_config_module(config_module="verl.trainer.config", version_base=None):
                    compose(config_name=config_name, overrides=without_plus)
            except ConfigCompositionException:
                pass
            else:
                raise AssertionError("native parameter_sync_step unexpectedly works without +")
        cls = get_trainer_cls(f"agentcore_{mode}")
        trainer = cls(cfg)
        assert trainer.trainer_mode == mode
        assert trainer.parameter_sync_step == 1
        assert trainer._num_actor_mini_batches == 2
        assert trainer._get_required_batch_multiple(dp_size=2) == 4
        assert issubclass(cls, module.RolloutFailureGuardMixin) == (mode == "sync")
        results.append({"mode": mode, "profile": profile, "tuning": tuning,
                        "config": config_name, "trainer": cls.__name__, "result": "PASS"})
        print(results[-1], flush=True)
    # Actual verl runtime capability gate for the pinned TQ, not source inference.
    gates = {"_tq_supports_checkpoint": trainer_base._tq_supports_checkpoint()}
    assert gates["_tq_supports_checkpoint"] is False
    print("TQ runtime gates:", gates)
    (WORK / "compose-results.json").write_text(json.dumps({"matrix": results, "gates": gates}, indent=2, default=str))
    print("PASS 8/8 real Hydra compositions and CPU registry constructors; skips=0")


if sys.argv[1] == "generate":
    generate()
elif sys.argv[1] == "check":
    check()
else:
    raise ValueError(sys.argv[1])
