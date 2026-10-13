import json
import time

import pytest
import yaml

from app.catalog.instances import CATALOG
from app.core.db import session_scope
from app.core.errors import AppError
from app.jobs import engine as eng
from app.models import Agent, AgentRuntime, Cluster, Dataset, Job, Run, RunMetric
from app.planner import plan
from app.render import train as rt
from app.services import runs as rsvc


def _hydra(params_b, itype, nodes=1, ctx=4096, moe=False, params=None, tuning="auto"):
    p = plan(
        params_b=params_b,
        spec=CATALOG[itype],
        nodes=nodes,
        max_model_len=ctx,
        is_moe=moe,
        tuning=tuning,
    ).to_dict()
    merged = rt.merge_params(
        {
            "max_model_len": ctx,
            "max_response_length": ctx,
            "max_prompt_length": min(2048, ctx),
            **(params or {}),
        }
    )
    h = rt.overrides(
        plan=p,
        params=merged,
        spec=CATALOG[itype],
        nodes=nodes,
        model_path="/fsx/models/m",
        train_file="/fsx/t.parquet",
        val_file="/fsx/v.parquet",
        ckpt_dir="/fsx/c",
        project="tp",
        experiment="run-1",
    )
    return dict(x.split("=", 1) for x in h)


def test_fsdp_matches_verified_qwen35_2b_script():
    h = _hydra(2.27, "p5.4xlarge")
    # experiments/qwen35_2b_gsm8k/trainer/train_qwen35_2b.sh
    expect = {
        "trainer.use_v1": "true",
        "trainer.v1.trainer_mode": "agentcore_sync",
        "algorithm.adv_estimator": "grpo",
        "algorithm.rollout_correction.rollout_is": "token",
        "algorithm.rollout_correction.rollout_is_threshold": "2.0",
        "data.custom_cls.name": "PayloadDataset",
        "data.train_batch_size": "32",
        "actor_rollout_ref.actor.optim.lr": "5e-06",
        "actor_rollout_ref.actor.ppo_mini_batch_size": "32",
        "actor_rollout_ref.actor.use_dynamic_bsz": "true",
        "actor_rollout_ref.actor.ppo_max_token_len_per_gpu": "8192",
        "actor_rollout_ref.actor.kl_loss_coef": "0.001",
        "actor_rollout_ref.actor.kl_loss_type": "low_var_kl",
        "actor_rollout_ref.actor.loss_agg_mode": "seq-mean-token-sum",
        "actor_rollout_ref.rollout.mode": "async",
        "actor_rollout_ref.rollout.calculate_log_probs": "true",
        "actor_rollout_ref.rollout.gpu_memory_utilization": "0.4",
        "actor_rollout_ref.rollout.n": "8",
        "actor_rollout_ref.rollout.val_kwargs.temperature": "0.6",
        "actor_rollout_ref.rollout.agent.num_workers": "1",
        "actor_rollout_ref.rollout.agent.default_agent_loop": "agentcore_agent",
        "trainer.n_gpus_per_node": "1",
        "trainer.nnodes": "1",
        "trainer.resume_mode": "auto",
        "actor_rollout_ref.model.enable_gradient_checkpointing": "true",
    }
    for k, v in expect.items():
        assert h[k] == v, (k, h.get(k), v)
    assert "actor_rollout_ref.actor.megatron.tensor_model_parallel_size" not in h
    assert "actor_rollout_ref.rollout.load_format" not in h


def test_fsdp_lora_loads_base_weights_from_disk():
    # backends/verl/examples/math_agent/fsdp_lora_sync_grpo.sh; the dummy default made
    # run-a1f2931440 (Qwen3.5-4B) fail in rebuild_ipc on the first weight sync.
    h = _hydra(4.66, "p5.48xlarge", ctx=32768, tuning="lora")
    assert h["actor_rollout_ref.model.lora_rank"] == "32"
    assert h["actor_rollout_ref.rollout.load_format"] == "safetensors"


def test_megatron_matches_verified_officebench_recipe():
    h = _hydra(
        27.8,
        "p5.48xlarge",
        ctx=131072,
        params={"max_prompt_length": 8192, "enable_thinking": False},
    )
    # backends/verl/examples/office_bench_agent/megatron_lora_sync_grpo_qwen3.6-27b.sh
    expect = {
        "actor_rollout_ref.actor.megatron.tensor_model_parallel_size": "4",
        "actor_rollout_ref.actor.megatron.context_parallel_size": "2",
        "actor_rollout_ref.actor.megatron.use_mbridge": "true",
        "actor_rollout_ref.actor.megatron.grad_offload": "true",
        "actor_rollout_ref.actor.megatron.optimizer_offload": "true",
        "actor_rollout_ref.model.lora.rank": "64",
        "actor_rollout_ref.model.lora.alpha": "128",
        "actor_rollout_ref.model.lora.merge": "true",
        "actor_rollout_ref.rollout.tensor_model_parallel_size": "4",
        "actor_rollout_ref.rollout.gpu_memory_utilization": "0.7",
        "actor_rollout_ref.actor.optim.lr": "1e-05",
        "actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu": "65536",
        "+data.apply_chat_template_kwargs.enable_thinking": "false",
        "trainer.n_gpus_per_node": "8",
    }
    for k, v in expect.items():
        assert h[k] == v, (k, h.get(k), v)


def test_multinode_counts_head_as_gpu_node():
    h = _hydra(30.5, "p5.48xlarge", nodes=2, ctx=32768, moe=True)
    assert h["trainer.nnodes"] == "2"
    assert h["actor_rollout_ref.actor.megatron.expert_model_parallel_size"] == "2"


def test_param_validation():
    with pytest.raises(AppError):
        rt.merge_params({"nope": 1})
    with pytest.raises(AppError):
        rt.merge_params({"rollout_n": 0})
    with pytest.raises(AppError):
        rt.merge_params({"max_prompt_length": 9000, "max_model_len": 4096})


def test_run_defaults_without_runtime_or_cloud_access(client):
    response = client.get("/api/runs/defaults")
    assert response.status_code == 200
    defaults = response.json()
    assert defaults["params"] == rt.merge_params({})
    assert defaults["agent_loop"] == {
        "max_tokens_per_turn": 1024,
        "tps_limit": 8,
        "max_rollout_time": 600,
    }
    for key in ("lr", "gpu_memory_utilization", "total_training_steps"):
        assert defaults["params"][key] is None
    loop = yaml.safe_load(rt.agent_loop_yaml({}, rt.merge_params({})))[0]
    for key, value in defaults["agent_loop"].items():
        assert loop[key] == value


@pytest.mark.parametrize(
    "template_id,tokens,timeout",
    [("gsm8k_math", 1024, 180), ("officebench", 8192, 1800)],
)
def test_template_defaults_match_rendered_loop(client, template_id, tokens, timeout):
    defaults = client.get("/api/runs/defaults").json()
    templates = client.get("/api/templates").json()
    template = next(t for t in templates if t["id"] == template_id)
    displayed = {k: template["agent_loop"].get(k, v) for k, v in defaults["agent_loop"].items()}
    assert displayed == {
        "max_tokens_per_turn": tokens,
        "tps_limit": 8,
        "max_rollout_time": timeout,
    }
    loop = yaml.safe_load(rt.agent_loop_yaml(template["agent_loop"], rt.merge_params({})))[0]
    for key, value in displayed.items():
        assert loop[key] == value

    explicit = {"max_tokens_per_turn": 512, "tps_limit": 2, "max_rollout_time": 90}
    loop = yaml.safe_load(rt.agent_loop_yaml(template["agent_loop"], rt.merge_params(explicit)))[0]
    for key, value in explicit.items():
        assert loop[key] == value
    # Resolving overrides must not mutate the defaults returned to a fresh form.
    assert client.get("/api/runs/defaults").json() == defaults


def test_cleared_params_restore_defaults_and_explicit_zero_is_preserved():
    defaults = rt.merge_params({})
    assert rt.merge_params({k: None for k in defaults}) == defaults
    explicit = {"temperature": 0, "val_temperature": 0, "kl_loss_coef": 0}
    merged = rt.merge_params(explicit)
    for key in explicit:
        assert merged[key] == 0
    h = _hydra(2.27, "p5.4xlarge", params=explicit)
    assert h["actor_rollout_ref.rollout.temperature"] == "0"
    assert h["actor_rollout_ref.rollout.val_kwargs.temperature"] == "0"
    assert h["actor_rollout_ref.actor.kl_loss_coef"] == "0"
    assert "trainer.total_training_steps" not in h


def test_agent_loop_yaml_forces_registered_sessions():
    y = yaml.safe_load(
        rt.agent_loop_yaml(
            {"max_tokens_per_turn": 8192, "history_mode": "linear", "linear_on_nonlinear": "reset"},
            rt.merge_params({}),
        )
    )
    loop = y[0]
    assert loop["require_registered_sessions"] is True
    assert loop["gateway_port"] == 18765
    assert loop["max_tokens_per_turn"] == 8192 and loop["history_mode"] == "linear"
    assert loop["agent_runtime_arn"] == "${oc.env:AGENT_RUNTIME_ARN}"
    assert "gateway_public_host" not in loop  # Ray node IP = VPC pod IP


def test_agent_loop_yaml_passes_failure_policy_from_template():
    keys = ("drop_agent_errors", "max_rollout_retries", "timeout_policy")
    loop = yaml.safe_load(rt.agent_loop_yaml({"max_tokens_per_turn": 8}, rt.merge_params({})))[0]
    assert not set(keys) & set(loop)  # unset: the toolkit's own defaults apply
    policy = {"drop_agent_errors": False, "max_rollout_retries": 0, "timeout_policy": "penalize"}
    loop = yaml.safe_load(
        rt.agent_loop_yaml({"max_tokens_per_turn": 8, **policy}, rt.merge_params({}))
    )[0]
    assert {k: loop[k] for k in keys} == policy


def test_train_script_quotes_and_stages_data():
    s = rt.train_script(
        run_id="run-1",
        bucket="b",
        region="us-east-1",
        model_id="Qwen/Qwen3.5-2B",
        model_dir="/fsx/models/Qwen--Qwen3.5-2B",
        data={"/fsx/runs/run-1/data/train.parquet": "s3://b/datasets/d/train.parquet"},
        hydra=["data.train_files=['/x']", 'trainer.logger=["console"]'],
        profile="megatron",
        run_dir="/fsx/runs/run-1",
    )
    assert "aws s3 cp --only-show-errors s3://b/datasets/d/train.parquet" in s
    assert "'data.train_files=['\"'\"'/x'\"'\"']'" in s  # shlex-quoted
    assert "apply-megatron-bridge-cp-clamp.sh" in s
    assert "s3://b/runs/run-1/logs/train.log" in s
    assert "python3 -m verl.trainer.main_ppo \\\n    --config-name ppo_megatron_trainer \\" in s
    fsdp = rt.train_script(
        run_id="run-1",
        bucket="b",
        region="us-east-1",
        model_id="m",
        model_dir="/fsx/models/m",
        data={},
        hydra=["a=1"],
        profile="fsdp",
        run_dir="/fsx/runs/run-1",
    )
    assert "--config-name" not in fsdp  # FSDP keeps verl's default ppo_trainer config


def test_ckpt_index_lists_only_steps_with_an_actor_dir(tmp_path):
    """verl's max_actor_ckpt_to_keep deletes global_step_N/actor but keeps the step dir;
    the index must not offer such steps for export (merge fails: actor not found)."""
    import subprocess

    s = rt.train_script(
        run_id="run-1",
        bucket="b",
        region="us-east-1",
        model_id="m",
        model_dir="/fsx/models/m",
        data={},
        hydra=[],
        profile="fsdp",
        run_dir=str(tmp_path),
    )
    fn = s[s.index("sync_progress() {") : s.index("\n}\n", s.index("sync_progress() {")) + 3]
    for step, actor in ((10, False), (20, True), (30, True)):
        (tmp_path / "ckpt" / f"global_step_{step}").mkdir(parents=True)
        if actor:
            (tmp_path / "ckpt" / f"global_step_{step}" / "actor").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "train.log").write_text("")
    bash = f"aws() {{ :; }}\nRUN_DIR={tmp_path}\nLOG=$RUN_DIR/logs/train.log\n{fn}\nsync_progress\n"
    subprocess.run(["bash", "-c", bash], check=True)
    index = json.loads((tmp_path / "logs" / "ckpt_index.json").read_text())
    assert index == {"steps": ["global_step_20", "global_step_30"]}
    assert "trap 'kill $SYNC_PID 2>/dev/null || true; sync_progress' EXIT" in s


def test_rayjob_single_and_multi_node():
    spec = CATALOG["p5.48xlarge"]
    env = rt.pod_env(
        run_id="r", region="us-east-1", bucket="b", runtime_arn="arn", multi_node=True, spec=spec
    )
    names = {e["name"] for e in env}
    assert {
        "AGENT_RUNTIME_ARN",
        "ACR_S3_BUCKET",
        "EXP_ID",
        "VERL_USE_EXTERNAL_MODULES",
        "FI_PROVIDER",
    } <= names
    envd = {e["name"]: e["value"] for e in env}
    assert envd["NCCL_DEBUG"] == "INFO" and envd["NCCL_DEBUG_SUBSYS"] == "INIT,NET"
    single = rt.pod_env(
        run_id="r", region="us-east-1", bucket="b", runtime_arn="arn", multi_node=False, spec=spec
    )
    assert {e["name"]: e["value"] for e in single}["NCCL_DEBUG"] == "WARN"
    one = rt.rayjob(
        run_id="r",
        name="r-a0",
        namespace="tuningpad",
        image="img",
        spec=spec,
        nodes=1,
        group="gpu",
        env=env,
        config_map="cm",
        service_account="sa",
        pvc="fsx",
    )
    head = one["spec"]["rayClusterSpec"]["headGroupSpec"]["template"]["spec"]
    res = head["containers"][0]["resources"]["limits"]
    assert res["nvidia.com/gpu"] == 8 and "vpc.amazonaws.com/efa" not in res
    assert head["nodeSelector"]["sagemaker.amazonaws.com/instance-group-name"] == "gpu"
    assert one["spec"]["rayClusterSpec"]["workerGroupSpecs"] == []
    sub = one["spec"]["submitterPodTemplate"]["spec"]
    assert sub["nodeSelector"]["sagemaker.amazonaws.com/instance-group-name"] == "system"
    multi = rt.rayjob(
        run_id="r",
        name="r-a0",
        namespace="tuningpad",
        image="img",
        spec=spec,
        nodes=3,
        group="gpu",
        env=env,
        config_map="cm",
        service_account="sa",
        pvc="fsx",
        active_deadline_s=3600,
    )
    w = multi["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
    assert w["replicas"] == 2
    assert (
        w["template"]["spec"]["containers"][0]["resources"]["limits"]["vpc.amazonaws.com/efa"] == 32
    )
    assert multi["spec"]["activeDeadlineSeconds"] == 3600


# ---------------- state machine ----------------


def _wait(job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with session_scope() as s:
            j = s.get(Job, job_id)
            if j.status in eng.TERMINAL:
                return j
        time.sleep(0.05)
    raise AssertionError("timeout")


@pytest.fixture
def run_env(monkeypatch):
    from app.pipelines import run as pr
    from app.services import project as proj

    proj.save_region("us-east-1", {"status": "ready", "bucket": "b", "trainer_repo_uri": "repo"})
    with session_scope() as s:
        s.add(
            Cluster(
                id="cl-1",
                name="dev",
                region="us-east-1",
                source="create",
                status="ready",
                hyperpod_name="tp-dev",
                eks_name="eks",
                network={},
                components={},
                params={},
                idle_policy={},
            )
        )
        s.add(
            Agent(
                id="ag-x",
                name="g",
                source="template",
                template_id="gsm8k_math",
                config={},
                status="ready",
                checks={},
            )
        )
        s.flush()
        s.add(
            AgentRuntime(
                id="rt-1",
                agent_id="ag-x",
                cluster_id="cl-1",
                region="us-east-1",
                runtime_arn="arn:rt",
                status="ready",
            )
        )
        s.add(
            Dataset(
                id="ds-1",
                name="g",
                region="us-east-1",
                source="upload",
                status="ready",
                splits={"train": {"s3_uri": "s3://b/t"}, "val": {"s3_uri": "s3://b/v"}},
                sample=[],
                stats={},
            )
        )
        s.flush()
        s.add(
            Run(
                id="run-1",
                name="r",
                agent_runtime_id="rt-1",
                cluster_id="cl-1",
                train_dataset_id="ds-1",
                model_id="Qwen/Qwen3.5-2B",
                spec={
                    "plan": plan(params_b=2.27, spec=CATALOG["p5.4xlarge"]).to_dict(),
                    "params": {},
                },
                compute={
                    "instance_group": "gpu",
                    "instance_type": "p5.4xlarge",
                    "nodes": 1,
                    "max_retries": 1,
                    "scale_down_after": False,
                },
                status="queued",
                progress={},
            )
        )
    monkeypatch.setattr(eng.StageContext, "sleep", lambda self, s: None)
    monkeypatch.setattr(pr, "POLL_S", 0)
    monkeypatch.setattr(pr.ptrainer, "find_or_create", lambda region, profile: ("ti-1", True))
    from app.models import TrainerImage

    with session_scope() as s:
        s.add(
            TrainerImage(
                id="ti-1",
                profile="fsdp",
                region="us-east-1",
                toolkit_sha="x",
                image_uri="img:fsdp",
                status="ready",
            )
        )
    hp_state = {"current": 0}
    group = {
        "InstanceGroupName": "gpu",
        "InstanceType": "ml.p5.4xlarge",
        "ExecutionRole": "r",
        "LifeCycleConfig": {},
    }

    def describe(region, name):
        g = {**group, "CurrentCount": hp_state["current"], "TargetCount": hp_state["current"]}
        return {
            "ClusterStatus": "InService",
            "InstanceGroups": [g],
            "NodeProvisioningMode": "Continuous",
        }

    def update(region, name, spec):
        hp_state["current"] = spec["InstanceCount"]

    monkeypatch.setattr(pr.hp, "describe_cluster", describe)
    monkeypatch.setattr(pr.hp, "update_group", update)
    monkeypatch.setattr(pr.hp, "latest_group_failure", lambda region, name, group: None)
    monkeypatch.setattr(pr.pc, "refresh_guardian", lambda cid: None)
    monkeypatch.setattr(pr.instances, "on_demand_price", lambda region, t: 8.256)
    applied, deleted = [], []
    states: list = []
    monkeypatch.setattr(pr.kube, "apply", lambda region, eks, m: applied.append(m) or m)
    monkeypatch.setattr(pr.kube, "get", lambda *a, **k: None)
    monkeypatch.setattr(
        pr.kube, "delete", lambda region, eks, api, kind, name, **k: deleted.append(name)
    )
    monkeypatch.setattr(
        pr.svc,
        "rayjob_state",
        lambda region, eks, name: states.pop(0)
        if states
        else {"job": "SUCCEEDED", "deployment": "Complete"},
    )
    log_lines = [
        "step:1 - critic/score/mean:0.25 - training/rollout_failure/total_missing_sessions:0\n"
        "step:2 - critic/score/mean:0.5 - val-core/unknown/reward/mean@1:np.float64(0.6)\n"
    ]
    monkeypatch.setattr(
        pr.svc,
        "read_log",
        lambda region, bucket, rid, off, max_bytes=0: (log_lines.pop(0), off + 100)
        if log_lines
        else ("", off),
    )
    monkeypatch.setattr(pr.svc, "ckpt_steps", lambda region, bucket, rid: [2])
    return {"applied": applied, "deleted": deleted, "states": states, "hp": hp_state}


def test_run_happy_path(run_env):
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.status == "succeeded", job.error
    kinds = [m["kind"] for m in run_env["applied"]]
    assert kinds == ["ConfigMap", "RayJob"]
    assert run_env["hp"]["current"] == 1  # scaled up to the requested nodes
    with session_scope() as s:
        r = s.get(Run, "run-1")
        assert r.status == "succeeded" and r.progress["step"] == 2
        assert r.progress["ckpt_steps"] == [2]
        assert r.est_cost_usd >= 0
        vals = {(m.step, m.key): m.value for m in s.query(RunMetric)}
    assert vals[(2, "val-core/unknown/reward/mean@1")] == 0.6
    cm = run_env["applied"][0]
    assert "require_registered_sessions: true" in cm["data"]["agentcore_agent.yaml"]
    # the finished RayJob is released right away (no 600 s TTL holding GPU nodes)
    rayjob = run_env["applied"][1]["metadata"]["name"]
    assert run_env["deleted"] == [rayjob]


def test_run_retries_then_fails(run_env):
    run_env["states"].extend(
        [
            {"job": "FAILED", "deployment": "Failed", "message": "oom"},
            {"job": "FAILED", "deployment": "Failed", "message": "oom"},
        ]
    )
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.status == "failed" and job.error_code == "run.failed"
    names = [m["metadata"]["name"] for m in run_env["applied"] if m["kind"] == "RayJob"]
    assert names == ["run-1-a0", "run-1-a1"]  # one retry, resumed from checkpoints
    # the failed attempt is replaced; the final failed RayJob is kept for debugging
    assert run_env["deleted"] == ["run-1-a0"]
    with session_scope() as s:
        r = s.get(Run, "run-1")
        assert r.status == "failed" and r.retries == 1


def test_run_budget_stop(run_env, monkeypatch):
    from app.pipelines import run as pr

    with session_scope() as s:
        r = s.get(Run, "run-1")
        r.compute = {**r.compute, "budget_usd": 0.0001}
    run_env["states"].extend([{"job": "RUNNING", "deployment": "Running"}] * 3)
    monkeypatch.setattr(
        pr, "_cost", lambda run, c: ({"node_hours": 1, "est_cost_usd": 9.0}, time.time())
    )
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.error_code == "run.budget_exceeded"
    assert run_env["deleted"] == ["run-1-a0"]


def test_rollout_failure_guard_fails_the_run_without_retry(run_env, monkeypatch):
    from app.pipelines import run as pr

    run_env["states"].append({"job": "FAILED", "deployment": "Failed", "message": "x"})
    log = (
        "old attempt: RolloutFailureGuardError from a previous attempt\n"
        f"{pr.ATTEMPT_MARK}\n"
        "step:3 - critic/score/mean:0.1 - training/rollout_failure/drop_fraction:0.75\n"
        "\x1b[36m(TaskRunnerV1 pid=5)\x1b[0m agentcore_rl_toolkit.backends.verl.trainer_mixins."
        "rollout_failure_guard.RolloutFailureGuardError: dropped 75% of rollouts\n"
    )
    monkeypatch.setattr(pr.svc, "read_log", lambda region, bucket, rid, off, **k: (log, off))
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.status == "failed" and job.error_code == "run.rollout_failure_guard"
    assert "dropped 75% of rollouts" in job.error and "\x1b" not in job.error
    names = [m["metadata"]["name"] for m in run_env["applied"] if m["kind"] == "RayJob"]
    assert names == ["run-1-a0"]  # no checkpoint resume: it would only replay the bug
    with session_scope() as s:
        assert s.get(Run, "run-1").retries == 0


def test_guard_line_of_an_older_attempt_does_not_block_retry(monkeypatch):
    from app.pipelines import run as pr

    log = f"RolloutFailureGuardError: x\n{pr.ATTEMPT_MARK}\nIndexError: oops\n"
    monkeypatch.setattr(pr.svc, "read_log", lambda region, bucket, rid, off, **k: (log, off))
    assert pr._non_retryable({"id": "run-1", "progress": {}}, "us-east-1", "b") is None


def test_cost_accrues_only_for_billed_nodes(monkeypatch):
    """Waiting with no instance (capacity timeout, Spot reclaim) costs nothing."""
    from app.pipelines import run as pr

    billed = {"n": 0}
    monkeypatch.setattr(pr.pools, "billed_nodes", lambda c, p, n: billed["n"])
    monkeypatch.setattr(pr.pools, "price_per_hour", lambda r, p, t, cap: 10.0)
    run = {
        "progress": {},
        "node_hours": None,
        "est_cost_usd": None,
        "compute": {"nodes": 2, "instance_group": "g", "instance_type": "p5.48xlarge"},
    }
    c = {"region": "us-east-2"}
    totals, now = pr._cost(run, c)  # first poll only sets the baseline
    assert totals == {"node_hours": 0.0, "est_cost_usd": 0.0}
    run["progress"] = {"cost_at": now - 3600}
    assert pr._cost(run, c)[0]["est_cost_usd"] == pytest.approx(0.0)
    billed["n"] = 5  # never more than the run's own nodes
    assert pr._cost(run, c)[0]["node_hours"] == pytest.approx(2.0, rel=1e-3)
    monkeypatch.setattr(pr.pools, "billed_nodes", lambda c, p, n: 1 / 0)
    run["node_hours"], run["est_cost_usd"] = 1.0, 10.0  # unknown → bill the request
    assert pr._cost(run, c)[0]["est_cost_usd"] == pytest.approx(30.0, rel=1e-3)


def test_faulty_nodes_maps_fault_lines_to_nodes():
    from app.pipelines import run as pr

    pods = [
        {"name": "run-1-a1-x-head-a", "type": "head", "ip": "10.0.0.1", "node": "n-head"},
        {"name": "run-1-a1-x-worker-b", "type": "worker", "ip": "10.0.0.2", "node": "n-w"},
    ]
    old = "RuntimeError: ... Error 802: system not yet initialized\n"
    log = (
        f"{old}{pr.ATTEMPT_MARK}\n"
        "(WorkerDict pid=7, ip=10.0.0.2) RuntimeError: CUDA error: GPU is lost\n"
        "RuntimeError: Unexpected error from cudaGetDeviceCount(). "
        "Error 802: system not yet initialized\n"
    )
    assert pr._faulty_nodes(log, pods) == {"n-head", "n-w"}
    assert pr._faulty_nodes(old + pr.ATTEMPT_MARK + "\nIndexError: oops\n", pods) == set()


def test_run_replaces_faulty_gpu_node_before_retry(run_env, monkeypatch):
    from app.pipelines import run as pr

    run_env["states"].append({"job": "FAILED", "deployment": "Failed", "message": "x"})
    fault = f"{pr.ATTEMPT_MARK}\nRuntimeError: ... Error 802: system not yet initialized\n"
    monkeypatch.setattr(pr.svc, "read_log", lambda region, bucket, rid, off, **k: (fault, off))
    monkeypatch.setattr(
        pr.svc,
        "run_pods",
        lambda region, eks, rid: [
            {"name": "run-1-a0-k-head-z", "type": "head", "ip": "10.0.0.9", "node": "n-bad"},
            {"name": "run-1-a1-k-head-y", "type": "head", "ip": "10.0.0.8", "node": "n-new"},
        ],
    )
    replaced = []
    monkeypatch.setattr(
        pr.pools,
        "replace_node",
        lambda c, provider, group, node: replaced.append((provider, group, node)) or "ok",
    )
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.status == "succeeded", job.error
    assert replaced == [("hyperpod", "gpu", "n-bad")]  # only the failed attempt's node
    names = [m["metadata"]["name"] for m in run_env["applied"] if m["kind"] == "RayJob"]
    assert names == ["run-1-a0", "run-1-a1"]


def test_scale_down_ignores_runs_on_other_pools(run_env):
    """Racing Spot and On-Demand pools: stopping the loser must drop its own pool to 0 even
    though the winner is busy on a sibling pool of the same cluster."""
    from app.pipelines import run as pr

    def add(rid, group):
        with session_scope() as s:
            s.add(
                Run(
                    id=rid,
                    name=rid,
                    agent_runtime_id="rt-1",
                    cluster_id="cl-1",
                    train_dataset_id="ds-1",
                    model_id="Qwen/Qwen3.5-2B",
                    spec={},
                    compute={"instance_group": group, "instance_type": "p5.4xlarge", "nodes": 1},
                    status="running",
                    progress={},
                )
            )

    c = {"id": "cl-1", "region": "us-east-1", "hyperpod_name": "hp"}
    add("run-od", "gpu-od")
    run_env["hp"]["current"] = 1
    logs: list[str] = []
    pr.scale_down_if_idle(c, "gpu", logs.append, pr.pools.HYPERPOD)
    assert run_env["hp"]["current"] == 0, logs  # the busy sibling pool does not hold it
    add("run-2", "gpu")
    run_env["hp"]["current"] = 1
    pr.scale_down_if_idle(c, "gpu", logs.append, pr.pools.HYPERPOD)
    assert run_env["hp"]["current"] == 1 and "run-2" in logs[-1]


def test_run_preflight_requires_ready_runtime(run_env):
    with session_scope() as s:
        s.get(AgentRuntime, "rt-1").status = "failed"
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.error_code == "run.runtime_not_ready"


def test_rayjob_name_is_dns_safe():
    assert rsvc.rayjob_name("run-AB12", 3) == "run-ab12-a3"


def test_log_pages_keep_utf8_characters_whole(stub_aws):
    """verl's progress bars are full of '│' (3 bytes): a page boundary inside one must not
    yield U+FFFD, and a full page must not report eof (run-210c57c0cb)."""
    import io

    from tests.conftest import StubClient

    body = ("x" * 9 + "│" + "tail\n").encode()  # '│' spans bytes 9..11

    def get_object(Bucket, Key, Range):
        a, b = (int(v) for v in Range.removeprefix("bytes=").split("-"))
        return {"Body": io.BytesIO(body[a : b + 1])}

    stub_aws({"s3": StubClient(get_object=get_object)})
    first, off = rsvc.read_log("us-east-1", "b", "run-1", 0, max_bytes=10)
    assert first == "x" * 9 and off == 9  # the split character waits for the next page
    rest, end = rsvc.read_log("us-east-1", "b", "run-1", off, max_bytes=10)
    assert first + rest == body.decode() and end == len(body) and "\ufffd" not in rest


def test_log_endpoint_eof_counts_bytes(client, run_env, monkeypatch):
    from app.routers import runs as rr

    page = 256 * 1024
    monkeypatch.setattr(rr.proj, "require_region", lambda region: {"bucket": "b"})
    chunk = "│" * (page // 3)  # a full page of bytes, only a third as many characters
    monkeypatch.setattr(
        rr.svc, "read_log", lambda r, b, rid, off, max_bytes: (chunk, off + len(chunk.encode()))
    )
    d = client.get("/api/runs/run-1/log?offset=0").json()
    assert d["eof"] is False and d["next_offset"] == len(chunk.encode())
    monkeypatch.setattr(rr.svc, "read_log", lambda r, b, rid, off, max_bytes: ("", off))
    assert client.get(f"/api/runs/run-1/log?offset={d['next_offset']}").json()["eof"] is True


def test_create_run_with_training_plan(client, run_env, monkeypatch):
    from app.routers import runs as rr

    # This tests training-plan validation/costs, not live Hugging Face model discovery.
    monkeypatch.setattr(
        rr.models,
        "check_model",
        lambda model_id: rr.models.ModelCheck(
            model_id=model_id,
            compatible=True,
            schema="qwen3_5",
            reason=None,
            template_source="chat_template.jinja",
            params=2_270_000_000,
            params_b=2.27,
            is_moe=False,
            num_experts=None,
            active_params_b=None,
            max_position_embeddings=32768,
        ),
    )
    calls = []

    def check(region, arn, itype, count, az_ids):
        calls.append((arn, itype, count))
        if itype != "p5.48xlarge":
            raise AppError("plan.instance_mismatch", "x")
        return {"arn": arn, "name": "ftp", "instance_type": "p5.48xlarge", "az_id": "use1-az4"}

    monkeypatch.setattr(rr.hp, "check_plan", check)
    body = {
        "name": "r",
        "agent_runtime_id": "rt-1",
        "train_dataset_id": "ds-1",
        "model_id": "Qwen/Qwen3.5-2B",
        "compute": {
            "instance_group": "gpu-ftp",
            "instance_type": "p5.48xlarge",
            "nodes": 1,
            "capacity": "training_plan",
            "max_hours": 2,
        },
    }
    r = client.post("/api/runs/preview", json=body)
    assert r.json()["code"] == "plan.required"
    body["compute"]["training_plan_arn"] = "arn:aws:sagemaker:us-east-1:1:training-plan/ftp"
    d = client.post("/api/runs/preview", json=body).json()
    assert d["estimate"]["prepaid"] and d["estimate"]["max_cost_usd"] == 0
    assert d["training_plan"]["name"] == "ftp"
    body["compute"]["instance_type"] = "p4d.24xlarge"
    assert client.post("/api/runs/preview", json=body).json()["code"] == "plan.instance_mismatch"
    # on-demand drops a stray plan ARN instead of validating it
    body["compute"].update(instance_type="p5.48xlarge", capacity="on_demand")
    n = len(calls)
    d = client.post("/api/runs/preview", json=body).json()
    assert d["training_plan"] is None and len(calls) == n and not d["estimate"]["prepaid"]


def test_run_resumes_after_node_replacement(run_env):
    """A HyperPod node replaced mid-run (auto-recovery or manual) kills its Ray pods: the
    RayJob fails, the monitor resubmits from the latest FSx checkpoint and the run ends
    succeeded."""
    run_env["states"].extend(
        [
            {"job": "RUNNING", "deployment": "Running"},
            {"job": "FAILED", "deployment": "Failed", "message": "worker node lost"},
        ]
    )
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.status == "succeeded", job.error
    names = [m["metadata"]["name"] for m in run_env["applied"] if m["kind"] == "RayJob"]
    assert names == ["run-1-a0", "run-1-a1"]
    # resubmission carries resume_mode=auto so verl picks up the latest checkpoint
    script = next(m for m in run_env["applied"] if m["kind"] == "ConfigMap")["data"]
    assert any("resume_mode=auto" in v for v in script.values())
    with session_scope() as s:
        r = s.get(Run, "run-1")
        assert r.status == "succeeded" and r.retries == 1


# ---------------- training-mode contracts (hermetic, not actual verl/GPU) ----------------

MODES = ["sync", "colocate_async"]
INVALID_MODES = [None, "", "separate_async", "unknown", 0, 1, True, [], {}]


def _mode_render(run, attempt=0):
    return rsvc.render(
        run,
        cluster={"region": "us-east-1"},
        runtime_arn="arn:rt",
        bucket="b",
        image="img",
        template_loop={},
        datasets={"train": "s3://b/train"},
        attempt=attempt,
    )


def _stored_mode(mode):
    with session_scope() as s:
        row = s.get(Run, "run-1")
        row.spec = {**row.spec, "training_mode": mode}
        row.compute = {**row.compute, "max_hours": 1}


@pytest.fixture
def mode_request(run_env, monkeypatch):
    from types import SimpleNamespace

    from app.routers import runs as rr

    # HF_HUB_OFFLINE alone is not a network sandbox: explicitly stub discovery.
    monkeypatch.setattr(
        rr.models,
        "check_model",
        lambda mid: SimpleNamespace(
            compatible=True, params_b=2.27, is_moe=False, to_dict=lambda: {"model_id": mid}
        ),
    )
    # Keep actual Run/Job persistence, but no pipeline work in API contract tests.
    monkeypatch.setattr(eng.get_engine()._pool, "submit", lambda *a, **k: None)
    return {
        "name": "mode-test",
        "agent_runtime_id": "rt-1",
        "train_dataset_id": "ds-1",
        "model_id": "Qwen/Qwen3.5-2B",
        "confirm_cost": True,
        "compute": {
            "instance_group": "gpu",
            "instance_type": "p5.4xlarge",
            "nodes": 1,
            "max_hours": 1,
        },
    }


def _assert_mode_rejected(client, body, code, run_env):
    with session_scope() as s:
        counts = (s.query(Run).count(), s.query(Job).count())
    for endpoint in ("/api/runs/preview", "/api/runs"):
        response = client.post(endpoint, json=body)
        assert response.status_code == (422 if code == "request.invalid" else 400), response.text
        assert response.json()["code"] == code
    with session_scope() as s:
        assert (s.query(Run).count(), s.query(Job).count()) == counts
    assert run_env["hp"]["current"] == 0
    assert not run_env["applied"]


@pytest.mark.parametrize("mode", ["omitted", *MODES])
def test_mode_api_persistence_round_trip(client, mode_request, mode):
    from app.pipelines.run import load_run

    if mode != "omitted":
        mode_request["training_mode"] = mode
    expected = "sync" if mode == "omitted" else mode
    preview = client.post("/api/runs/preview", json=mode_request)
    assert preview.status_code == 200, preview.text
    assert preview.json()["training_mode"] == expected
    created = client.post("/api/runs", json=mode_request)
    assert created.status_code == 200, created.text
    rid, jid = created.json()["id"], created.json()["job_id"]
    # Reopen DB, then traverse the public detail endpoint and real renderer.
    with session_scope() as s:
        row = s.get(Run, rid)
        assert row.spec["training_mode"] == expected
        assert s.get(Job, jid).target_id == rid
    assert client.get(f"/api/runs/{rid}").json()["spec"]["training_mode"] == expected
    assert f"trainer.v1.trainer_mode=agentcore_{expected}" in _mode_render(load_run(rid))["hydra"]


@pytest.mark.parametrize("mode", INVALID_MODES)
def test_invalid_mode_requests_have_no_side_effects(client, mode_request, run_env, mode):
    mode_request["training_mode"] = mode
    _assert_mode_rejected(client, mode_request, "request.invalid", run_env)


@pytest.mark.parametrize("container", ["params", "plan_overrides"])
@pytest.mark.parametrize(
    "key",
    [
        "training_mode",
        "trainer_mode",
        "trainer.v1.trainer_mode",
        "+trainer.v1.colocate_async.parameter_sync_step",
    ],
)
def test_mode_and_hydra_injection_rejected(client, mode_request, run_env, container, key):
    mode_request[container] = {key: "colocate_async"}
    code = "run.unknown_param" if container == "params" else "run.bad_plan_override"
    _assert_mode_rejected(client, mode_request, code, run_env)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "override",
    [
        {"profile": "megatron"},
        {"strategy": "megatron_lora"},
        {"profile": None},
        {"profile": []},
        {"strategy": {}},
        {"strategy": "fsdp_bad"},
    ],
)
def test_final_plan_pair_is_checked(client, mode_request, run_env, mode, override):
    mode_request.update(training_mode=mode, plan_overrides=override)
    _assert_mode_rejected(client, mode_request, "run.bad_plan_override", run_env)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("key", ["train_batch_size", "ppo_mini_batch_size"])
@pytest.mark.parametrize("value", [0, -1, 0.5, 8.0, True, False, "8", [], {}])
def test_batch_strict_integers_at_api(client, mode_request, run_env, mode, key, value):
    mode_request.update(training_mode=mode, params={key: value})
    _assert_mode_rejected(client, mode_request, "run.invalid_batch", run_env)


@pytest.mark.parametrize("mode", MODES)
def test_indivisible_batch_at_api(client, mode_request, run_env, mode):
    mode_request.update(
        training_mode=mode, params={"train_batch_size": 8, "ppo_mini_batch_size": 3}
    )
    _assert_mode_rejected(client, mode_request, "run.invalid_batch", run_env)


@pytest.mark.parametrize(
    "hours", ["omitted", None, 0, -1, 1 / 7200, 721, "NaN", "Infinity", "-Infinity"]
)
def test_async_deadline_at_api(client, mode_request, run_env, hours):
    mode_request["training_mode"] = "colocate_async"
    if hours == "omitted":
        mode_request["compute"].pop("max_hours")
    else:
        mode_request["compute"]["max_hours"] = hours
    code = (
        "run.async_deadline_required" if hours in ("omitted", None, 1 / 7200) else "request.invalid"
    )
    _assert_mode_rejected(client, mode_request, code, run_env)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("literal, encoded", [("1e999", "inf"), ("-1e999", "-inf")])
def test_overflow_deadline_json_is_422_without_side_effects(
    client, mode_request, run_env, mode, literal, encoded
):
    mode_request["training_mode"] = mode
    mode_request["compute"]["max_hours"] = "RAW_HOURS"
    # Legal JSON numbers can overflow Python floats; json= would reject them client-side.
    body = json.dumps(mode_request).replace('"RAW_HOURS"', literal)
    with session_scope() as s:
        counts = (s.query(Run).count(), s.query(Job).count())
    for endpoint in ("/api/runs/preview", "/api/runs"):
        response = client.post(endpoint, content=body, headers={"Content-Type": "application/json"})
        assert response.status_code == 422, response.text
        error = response.json()
        assert error["code"] == "request.invalid"
        detail = error["detail"][0]
        assert detail["loc"] == ["body", "compute", "max_hours"]
        assert detail["input"] == encoded
        assert isinstance(next(iter(detail["ctx"].values())), (int, float))
    with session_scope() as s:
        assert (s.query(Run).count(), s.query(Job).count()) == counts
    assert run_env["hp"]["current"] == 0
    assert not run_env["applied"]


@pytest.mark.parametrize("hours", [None, 1 / 3600, 720])
@pytest.mark.parametrize("mode", MODES)
def test_valid_deadline_api_and_cost_confirmation(client, mode_request, run_env, hours, mode):
    mode_request.update(training_mode=mode, confirm_cost=False)
    mode_request["compute"]["max_hours"] = hours
    response = client.post("/api/runs/preview", json=mode_request)
    if mode == "colocate_async" and hours is None:
        assert response.json()["code"] == "run.async_deadline_required"
        return
    assert response.status_code == 200, response.text
    rejected = client.post("/api/runs", json=mode_request)
    assert rejected.json()["code"] == "run.confirm_cost"
    with session_scope() as s:
        assert s.query(Run).count() == 1 and s.query(Job).count() == 0
    assert not run_env["applied"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("profile", ["fsdp", "megatron"])
@pytest.mark.parametrize("tuning", ["full", "lora"])
def test_two_modes_four_strategy_render_contract(run_env, mode, profile, tuning):
    from app.pipelines.run import load_run

    run = load_run("run-1")
    run["compute"].update(instance_type="p5.48xlarge", nodes=2, max_hours=1)
    run["spec"].update(
        training_mode=mode,
        plan=plan(
            params_b=4, spec=CATALOG["p5.48xlarge"], nodes=2, prefer=profile, tuning=tuning
        ).to_dict(),
        params={
            "train_batch_size": 8,
            "ppo_mini_batch_size": 4,
            "temperature": 0,
            "val_temperature": 0,
            "kl_loss_coef": 0,
        },
    )
    objs = _mode_render(run)
    h = dict(x.split("=", 1) for x in objs["hydra"])
    assert h["trainer.v1.trainer_mode"] == f"agentcore_{mode}"
    assert h["trainer.use_v1"] == "true" and h["algorithm.adv_estimator"] == "grpo"
    assert h["actor_rollout_ref.actor.loss_agg_mode"] == "seq-mean-token-sum"
    assert h["actor_rollout_ref.rollout.mode"] == "async"
    assert h["actor_rollout_ref.rollout.calculate_log_probs"] == "true"
    assert h["algorithm.rollout_correction.rollout_is"] == "token"
    assert h["algorithm.rollout_correction.rollout_is_threshold"] == "2.0"
    assert (
        h["data.train_batch_size"] == "8"
        and h["actor_rollout_ref.actor.ppo_mini_batch_size"] == "4"
    )
    for key in (
        "actor_rollout_ref.rollout.temperature",
        "actor_rollout_ref.rollout.val_kwargs.temperature",
        "actor_rollout_ref.actor.kl_loss_coef",
    ):
        assert h[key] == "0"
    async_keys = {
        "trainer.v1.colocate_async.num_warmup_batches": "1",
        "trainer.v1.sampler.max_off_policy_threshold": "8",
        "trainer.v1.sampler.max_off_policy_strategy": "drop",
        "+trainer.v1.colocate_async.parameter_sync_step": "1",
    }
    if mode == "colocate_async":
        assert {key: h[key] for key in async_keys} == async_keys
    else:
        assert not async_keys.keys() & h.keys()
    assert not any("agentcore_colocate_async." in key for key in h)
    assert h["actor_rollout_ref.actor.use_dynamic_bsz"] == str(profile == "fsdp").lower()
    if profile == "megatron":
        assert h["actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu"] == "1"
        assert h["actor_rollout_ref.actor.checkpoint.save_contents"] == '["model"]'
    if tuning == "lora":
        if profile == "fsdp":
            assert h["actor_rollout_ref.rollout.load_format"] == "safetensors"
            assert h["actor_rollout_ref.model.lora_rank"] == "32"
        else:
            assert h["actor_rollout_ref.model.lora.merge"] == "true"
            assert h["actor_rollout_ref.model.lora.rank"] == "64"
    script = objs["config_map"]["data"]["train.sh"]
    assert ("--config-name ppo_megatron_trainer" in script) == (profile == "megatron")
    loop = yaml.safe_load(objs["config_map"]["data"]["agentcore_agent.yaml"])[0]
    assert loop["require_registered_sessions"] is True
    job = objs["rayjob"]["spec"]
    assert job["activeDeadlineSeconds"] == 3600
    head = job["rayClusterSpec"]["headGroupSpec"]["template"]["spec"]
    env = {e["name"]: e.get("value") for e in head["containers"][0]["env"]}
    assert env["VERL_USE_EXTERNAL_MODULES"] == "agentcore_rl_toolkit.backends.verl.trainer"
    assert head["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 8
    assert job["rayClusterSpec"]["workerGroupSpecs"][0]["replicas"] == 1
    assert h["trainer.nnodes"] == "2" and h["trainer.n_gpus_per_node"] == "8"


@pytest.mark.parametrize("mode", ["legacy", *MODES])
@pytest.mark.parametrize(
    "itype,nodes,units,cpu,gpus",
    [
        ("p5.4xlarge", 1, 2, 8, 1),
        ("p5.4xlarge", 2, None, 8, 1),
        ("p5.48xlarge", 1, None, 184, 8),
        ("p5.48xlarge", 2, None, 184, 8),
        ("p4d.24xlarge", 1, None, 88, 8),
    ],
)
def test_transfer_queue_cpu_budget_preserves_resources(
    run_env, mode, itype, nodes, units, cpu, gpus
):
    from app.pipelines.run import load_run

    run = load_run("run-1")
    run["compute"].update(instance_type=itype, nodes=nodes, max_hours=1)
    run["spec"]["plan"] = plan(params_b=2.27, spec=CATALOG[itype], nodes=nodes).to_dict()
    if mode != "legacy":
        run["spec"]["training_mode"] = mode
    rendered = _mode_render(run)
    h = dict(x.split("=", 1) for x in rendered["hydra"])
    key = "transfer_queue.backend.SimpleStorage.num_data_storage_units"
    if units is None:
        assert key not in h  # preserve upstream 8; do not pin large/multinode defaults
    else:
        assert h[key] == str(units)
        assert units + 1 + 3 * gpus + 2 == cpu  # controller + training PG + admission/spare
        assert f"{key}={units}" in rendered["config_map"]["data"]["train.sh"]
    assert not any(k.startswith("ray_kwargs.") for k in h)
    assert "transfer_queue.enable" not in h  # V1 forces enable for both modes
    assert "transfer_queue.backend.SimpleStorage.total_storage_size" not in h
    assert h["actor_rollout_ref.rollout.agent.num_workers"] == "1"
    assert h["trainer.nnodes"] == str(nodes) and h["trainer.n_gpus_per_node"] == str(gpus)
    job = rendered["rayjob"]["spec"]
    assert job["activeDeadlineSeconds"] == 3600
    cluster = job["rayClusterSpec"]
    groups = [cluster["headGroupSpec"], *cluster["workerGroupSpecs"]]
    assert sum(g.get("replicas", 1) for g in groups) == nodes
    for group in groups:
        assert "num-cpus" not in group["rayStartParams"]  # no fake CPU advertising
        assert group["rayStartParams"]["num-gpus"] == str(gpus)
        res = group["template"]["spec"]["containers"][0]["resources"]
        assert res["requests"] == res["limits"]
        assert res["limits"]["cpu"] == str(cpu)
        assert res["limits"]["nvidia.com/gpu"] == gpus
    assert not any("storage_units" in key for key in rt.DEFAULT_PARAMS)


@pytest.mark.parametrize(
    "vcpus,expected", [(16, "2"), (18, "4"), (21, "7"), (22, None), (32, None)]
)
def test_transfer_queue_single_node_override_tracks_pod_cpu_budget(vcpus, expected):
    from dataclasses import replace

    spec = replace(CATALOG["p5.4xlarge"], vcpus=vcpus)
    h = dict(
        x.split("=", 1)
        for x in rt.overrides(
            plan=plan(params_b=2.27, spec=spec).to_dict(),
            params=rt.merge_params({}),
            spec=spec,
            nodes=1,
            model_path="m",
            train_file="t",
            val_file=None,
            ckpt_dir="c",
            project="p",
            experiment="e",
        )
    )
    assert h.get("transfer_queue.backend.SimpleStorage.num_data_storage_units") == expected


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("mini", ["omitted", None, 1, 4, 8])
def test_mini_batch_inheritance(run_env, mode, mini):
    from app.pipelines.run import load_run

    run = load_run("run-1")
    run["compute"]["max_hours"] = 1
    params = {"train_batch_size": 8}
    if mini != "omitted":
        params["ppo_mini_batch_size"] = mini
    run["spec"].update(training_mode=mode, params=params)
    h = dict(x.split("=", 1) for x in _mode_render(run)["hydra"])
    expected = 8 if mini in ("omitted", None) else mini
    assert h["actor_rollout_ref.actor.ppo_mini_batch_size"] == str(expected)


@pytest.mark.parametrize("mode", ["legacy", *MODES])
@pytest.mark.parametrize("hours", ["omitted", None, 1 / 3600, 720])
def test_manifest_deadline_boundaries(run_env, mode, hours):
    from app.pipelines.run import load_run

    run = load_run("run-1")
    if mode != "legacy":
        run["spec"]["training_mode"] = mode
    if hours != "omitted":
        run["compute"]["max_hours"] = hours
    if mode == "colocate_async" and hours in ("omitted", None):
        with pytest.raises(AppError, match="at least one second"):
            _mode_render(run)
    else:
        job = _mode_render(run)["rayjob"]["spec"]
        if hours in ("omitted", None):
            assert "activeDeadlineSeconds" not in job
        else:
            assert type(job["activeDeadlineSeconds"]) is int
            assert job["activeDeadlineSeconds"] == int(hours * 3600)


@pytest.mark.parametrize("key", ["train_batch_size", "ppo_mini_batch_size"])
@pytest.mark.parametrize("value", [0, -1, 2.5, True, "8", 3])
def test_low_level_renderer_rejects_invalid_batches(key, value):
    params = {**rt.DEFAULT_PARAMS, "train_batch_size": 8, "ppo_mini_batch_size": 4, key: value}
    with pytest.raises(AppError) as error:
        rt.overrides(
            plan=plan(params_b=2, spec=CATALOG["p5.4xlarge"]).to_dict(),
            params=params,
            spec=CATALOG["p5.4xlarge"],
            nodes=1,
            model_path="m",
            train_file="t",
            val_file=None,
            ckpt_dir="c",
            project="p",
            experiment="e",
        )
    assert error.value.code == "run.invalid_batch"


@pytest.mark.parametrize("mode", INVALID_MODES)
def test_invalid_stored_mode_never_falls_back(client, run_env, mode):
    from app.pipelines import run as pr

    _stored_mode(mode)
    original = pr.load_run("run-1")["spec"]
    with pytest.raises(AppError) as error:
        _mode_render(pr.load_run("run-1"))
    assert error.value.code == "run.invalid_training_mode"
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.error_code == "run.invalid_training_mode"
    assert job.stages[0]["status"] == "failed"
    assert all(stage["status"] == "pending" for stage in job.stages[1:])
    assert not run_env["applied"] and run_env["hp"]["current"] == 0
    # Resume cannot write a new Job or change the original invalid spec either.
    response = client.post("/api/runs/run-1/resume")
    assert response.json()["code"] == "run.invalid_training_mode"
    with session_scope() as s:
        assert s.query(Job).count() == 1
        assert s.get(Run, "run-1").spec == original
        assert s.get(Run, "run-1").status == "failed"


@pytest.mark.parametrize("stage", ["stage_preflight", "stage_image", "stage_capacity"])
@pytest.mark.parametrize(
    "field,value,code",
    [
        ("training_mode", None, "run.invalid_training_mode"),
        ("params", {"ppo_mini_batch_size": 0}, "run.invalid_batch"),
        ("params", {"ppo_mini_batch_size": 3}, "run.invalid_batch"),
        ("plan", {"profile": "fsdp", "strategy": "megatron_full"}, "run.bad_plan_override"),
        ("max_hours", None, "run.async_deadline_required"),
        ("max_hours", 0, "run.async_deadline_required"),
        ("max_hours", -1, "run.async_deadline_required"),
        ("max_hours", 1 / 7200, "run.async_deadline_required"),
        ("max_hours", 721, "run.async_deadline_required"),
        ("max_hours", float("inf"), "run.async_deadline_required"),
        ("max_hours", float("-inf"), "run.async_deadline_required"),
        ("max_hours", float("nan"), "run.async_deadline_required"),
    ],
)
def test_stored_contract_rejected_before_external_work(
    run_env, monkeypatch, stage, field, value, code
):
    from types import SimpleNamespace

    from app.pipelines import run as pr

    _stored_mode("colocate_async")
    with session_scope() as s:
        row = s.get(Run, "run-1")
        if field == "max_hours":
            row.compute = {**row.compute, field: value}
        else:
            row.spec = {**row.spec, field: value}

    def forbidden(*args, **kwargs):
        raise AssertionError("contract validation must precede context/image/capacity work")

    monkeypatch.setattr(pr, "_context", forbidden)
    monkeypatch.setattr(pr.pc, "load", forbidden)
    with pytest.raises(AppError) as error:
        getattr(pr, stage)(SimpleNamespace(target_id="run-1"))
    assert error.value.code == code
    with pytest.raises(AppError) as error:
        _mode_render(pr.load_run("run-1"))
    assert error.value.code == code
    with session_scope() as s:
        assert s.get(Run, "run-1").status == "queued"
        assert s.query(Job).count() == 0
    assert not run_env["applied"] and run_env["hp"]["current"] == 0


def test_legacy_sync_is_read_only_and_identical_to_explicit_sync(client, run_env):
    from copy import deepcopy

    from app.pipelines import run as pr

    original = pr.load_run("run-1")
    explicit = deepcopy(original)
    explicit["spec"]["training_mode"] = "sync"
    assert _mode_render(original) == _mode_render(explicit)
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.status == "succeeded", job.error
    assert "training_mode" not in client.get("/api/runs/run-1").json()["spec"]
    with session_scope() as s:
        assert s.get(Run, "run-1").spec == original["spec"]


@pytest.mark.parametrize("mode", MODES)
def test_retry_and_manual_resume_preserve_original_mode(client, run_env, mode):
    _stored_mode(mode)
    with session_scope() as s:
        original = s.get(Run, "run-1").spec
    run_env["states"].append({"job": "FAILED", "deployment": "Failed", "message": "worker lost"})
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.status == "succeeded", job.error
    response = client.post("/api/runs/run-1/resume")
    assert response.status_code == 200, response.text
    resumed = _wait(response.json()["job_id"])
    assert resumed.status == "succeeded", resumed.error
    cms = [obj for obj in run_env["applied"] if obj["kind"] == "ConfigMap"]
    jobs = [obj for obj in run_env["applied"] if obj["kind"] == "RayJob"]
    assert len(cms) == 3
    assert [j["metadata"]["name"] for j in jobs] == ["run-1-a0", "run-1-a1", "run-1-a2"]
    for cm in cms:
        hydra = json.loads(cm["data"]["hydra.json"])
        assert f"trainer.v1.trainer_mode=agentcore_{mode}" in hydra
        assert "trainer.resume_mode=auto" in hydra
    # Deadline is per attempt; retries do not consume a shared time budget.
    assert [j["spec"]["activeDeadlineSeconds"] for j in jobs] == [3600] * 3
    with session_scope() as s:
        assert s.get(Run, "run-1").spec == original


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("already_submitted", [False, True])
def test_backend_restart_keeps_mode_and_does_not_duplicate_submission(
    run_env, mode, already_submitted
):
    _stored_mode(mode)
    with session_scope() as s:
        row = s.get(Run, "run-1")
        original = row.spec
        if already_submitted:
            row.rayjob_name = "run-1-a0"
            row.progress = {"attempt": 0}
        s.add(
            Job(
                id="job-restart",
                type="run.train",
                target_id="run-1",
                status="running",
                payload={},
                context={"image": "img:fsdp"},
                stages=[
                    {
                        "name": name,
                        "status": "succeeded"
                        if name in ("preflight", "image", "capacity")
                        else "pending",
                    }
                    for name in ("preflight", "image", "capacity", "submit", "monitor", "finalize")
                ],
            )
        )
    eng.get_engine().shutdown()
    eng.set_engine(eng.JobEngine(max_workers=2))
    assert eng.get_engine().resume_pending() == ["job-restart"]
    job = _wait("job-restart")
    assert job.status == "succeeded", job.error
    cms = [obj for obj in run_env["applied"] if obj["kind"] == "ConfigMap"]
    if already_submitted:
        assert not run_env["applied"]
    else:
        assert len(cms) == 1
        assert f"trainer.v1.trainer_mode=agentcore_{mode}" in json.loads(
            cms[0]["data"]["hydra.json"]
        )
    with session_scope() as s:
        assert s.get(Run, "run-1").spec == original
