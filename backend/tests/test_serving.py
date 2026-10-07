import pytest

from app.core.errors import AppError
from app.services import serving as svc


def test_merge_job_command_and_placement():
    j = svc.merge_job(
        export_id="ex-1",
        run_id="run-1",
        step=10,
        backend="fsdp",
        image="img",
        bucket="b",
        group="system",
        region="us-east-1",
        mem_gib=16,
    )
    c = j["spec"]["template"]["spec"]["containers"][0]
    script = c["command"][2]
    assert "verl.model_merger merge --backend fsdp" in script
    assert "--local_dir /fsx/runs/run-1/ckpt/global_step_10/actor" in script
    assert "s3://b/exports/ex-1/" in script
    # CPU-only: no GPU injection on the GPU-less system node
    assert {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"} in c["env"]
    assert (
        j["spec"]["template"]["spec"]["nodeSelector"]["sagemaker.amazonaws.com/instance-group-name"]
        == "system"
    )


@pytest.mark.parametrize("params_b,group", [(2.3, "system"), (4.0, "system"), (27.8, "gpu")])
def test_merge_resources(params_b, group):
    assert svc.merge_resources(params_b, "gpu")[0] == group


def test_vllm_manifests_internal_nlb_with_api_key():
    dep, service = svc.vllm_manifests(
        endpoint_id="ep-1",
        model="/fsx/exports/ex-1",
        served_name="run-step10",
        group="gpu",
        instance_type="p5.48xlarge",
        tp=2,
        replicas=1,
        tool_parser="qwen3_coder",
        reasoning_parser="qwen3",
        sg_nlb="sg-nlb",
        subnets=["s1", "s2"],
        max_model_len=8192,
    )
    args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[args.index("--api-key") + 1] == "$(VLLM_API_KEY)"
    assert args[args.index("--tool-call-parser") + 1] == "qwen3_coder"
    assert "--enable-auto-tool-choice" in args
    ann = service["metadata"]["annotations"]
    assert ann["service.beta.kubernetes.io/aws-load-balancer-scheme"] == "internal"
    assert ann["service.beta.kubernetes.io/aws-load-balancer-security-groups"] == "sg-nlb"
    assert (
        dep["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"]
        == 2
    )
    with pytest.raises(AppError):
        svc.vllm_manifests(
            endpoint_id="e",
            model="m",
            served_name="m",
            group="g",
            instance_type="p5.4xlarge",
            tp=2,
            replicas=1,
            tool_parser=None,
            reasoning_parser=None,
            sg_nlb="sg",
            subnets=[],
            max_model_len=None,
        )


def test_api_key_is_random():
    assert svc.api_key() != svc.api_key() and svc.api_key().startswith("tp-")


def test_summarize_eval_counts_failures_as_zero():
    items = [
        {"success": True, "result": {"rewards": 1.0}},
        {"success": True, "result": {"rewards": [0, 1]}},
        {"success": True, "result": {"rewards": 0}},
        {"success": False, "error": "timeout"},
    ]
    s = svc.summarize_eval(items)
    assert s["n"] == 4 and s["scored"] == 3 and s["failed"] == 1
    assert s["mean_reward"] == 0.5 and s["mean_reward_scored"] == pytest.approx(0.6667, abs=1e-3)
    assert s["acr_failed_rate"] == 0.25 and s["truncated"] == 0


def test_summarize_eval_scores_truncation_as_wrong_not_failed():
    strands_500 = "Agent has reached an unrecoverable state due to max_tokens limit."
    items = [
        {"success": True, "result": {"rewards": 1.0, "stop_reason": "end_turn"}},
        {"success": True, "result": {"rewards": 0.0, "stop_reason": "max_tokens"}},
        {"success": True, "result": {"status_code": 500, "stop_reason": strands_500}},
        {"success": False, "error": "timeout"},
    ]
    s = svc.summarize_eval(items)
    assert (s["scored"], s["truncated"], s["failed"]) == (3, 2, 1)
    assert s["mean_reward"] == 0.25 and s["acr_failed_rate"] == 0.25


def test_eval_sampling_matches_training_validation():
    # template default per-turn budget + run's val temperature
    s = svc.eval_sampling({"max_tokens_per_turn": 1024}, {"val_temperature": 0.3})
    assert s == {"max_tokens": 1024, "temperature": 0.3}
    # a run-level max_tokens_per_turn wins over the template
    assert (
        svc.eval_sampling({"max_tokens_per_turn": 1024}, {"max_tokens_per_turn": 2048})[
            "max_tokens"
        ]
        == 2048
    )
    # no template, no run (HF endpoint): toolkit default 1024, verl val default 0.6
    assert svc.eval_sampling(None, None) == {"max_tokens": 1024, "temperature": 0.6}
    # explicit overrides; unknown keys ignored
    s = svc.eval_sampling(None, None, {"temperature": 0.0, "top_p": 0.95, "seed": 1})
    assert s == {"max_tokens": 1024, "temperature": 0.0, "top_p": 0.95}
