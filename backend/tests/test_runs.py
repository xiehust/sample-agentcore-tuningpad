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


def _hydra(params_b, itype, nodes=1, ctx=4096, moe=False, params=None):
    p = plan(
        params_b=params_b, spec=CATALOG[itype], nodes=nodes, max_model_len=ctx, is_moe=moe
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
    assert "python3 -m verl.trainer.main_ppo" in s


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
    monkeypatch.setattr(pr, "_cost", lambda run, region: {"node_hours": 1, "est_cost_usd": 9.0})
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.error_code == "run.budget_exceeded"
    assert run_env["deleted"] == ["run-1-a0"]


def test_run_preflight_requires_ready_runtime(run_env):
    with session_scope() as s:
        s.get(AgentRuntime, "rt-1").status = "failed"
    job = _wait(eng.get_engine().start("run.train", "run-1", {}))
    assert job.error_code == "run.runtime_not_ready"


def test_rayjob_name_is_dns_safe():
    assert rsvc.rayjob_name("run-AB12", 3) == "run-ab12-a3"


def test_create_run_with_training_plan(client, run_env, monkeypatch):
    from app.routers import runs as rr

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
