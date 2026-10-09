from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from ..catalog import instances, models
from ..core.db import new_id, session_scope
from ..core.errors import AppError, NotFound
from ..jobs.engine import get_engine, job_view, latest_job
from ..metrics.verl_log import KEY_METRICS, summarize
from ..models import AgentRuntime, Dataset, Run, RunMetric
from ..pipelines import cluster as pc
from ..pipelines.run import load_run
from ..planner import plan as make_plan
from ..render.train import AGENT_LOOP_DEFAULTS, DEFAULT_PARAMS, merge_params
from ..services import hyperpod as hp
from ..services import pools
from ..services import project as proj
from ..services import runs as svc
from ..templates_lib import get_template

router = APIRouter(prefix="/api/runs", tags=["runs"])
ACTIVE = {"queued", "preparing", "waiting_capacity", "running", "retrying"}


class Compute(BaseModel):
    instance_group: str = Field(pattern=r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}$")
    instance_type: str
    nodes: int = Field(1, ge=1, le=20)
    # hyperpod: HyperPod instance group · ec2: EKS managed node group (plain EC2)
    provider: str = Field("hyperpod", pattern="^(hyperpod|ec2)$")
    capacity: str = Field("on_demand", pattern="^(on_demand|training_plan|spot)$")
    training_plan_arn: str | None = None
    scale_up: bool = True
    scale_down_after: bool = True
    max_hours: float | None = Field(None, gt=0, le=720)
    budget_usd: float | None = Field(None, gt=0)
    max_retries: int = Field(2, ge=0, le=10)

    @model_validator(mode="after")
    def _ec2_capacity(self):
        if self.provider == "ec2" and self.capacity == "training_plan":
            raise ValueError("EC2 node groups use on_demand or spot capacity")
        return self


class CreateRun(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    agent_runtime_id: str
    train_dataset_id: str
    val_dataset_id: str | None = None
    val_split: str = "val"
    model_id: str
    prefer: str = Field("auto", pattern="^(auto|fsdp|megatron)$")
    tuning: str = Field("auto", pattern="^(auto|full|lora)$")
    params: dict[str, Any] = Field(default_factory=dict)
    plan_overrides: dict[str, Any] = Field(default_factory=dict)
    compute: Compute
    confirm_cost: bool = False


def _estimate(region: str, compute: Compute, params: dict[str, Any]) -> dict[str, Any]:
    # training-plan capacity is paid upfront: the run adds no per-hour GPU cost
    prepaid = compute.capacity == "training_plan"
    price = pools.price_per_hour(region, compute.provider, compute.instance_type, compute.capacity)
    hours = compute.max_hours
    return {
        "prepaid": prepaid,
        "price_per_node_hour": price,
        "nodes": compute.nodes,
        "hourly_usd": round(price * compute.nodes, 2),
        "max_hours": hours,
        "max_cost_usd": round(price * compute.nodes * hours, 2) if hours else None,
    }


def _validate(body: CreateRun) -> dict[str, Any]:
    with session_scope() as s:
        rt = s.get(AgentRuntime, body.agent_runtime_id)
        if not rt or not rt.cluster_id:
            raise AppError("run.bad_runtime", "pick an agent runtime deployed to a cluster")
        if rt.status != "ready":
            raise AppError("run.runtime_not_ready", "the agent runtime is not ready")
        cluster_id, region = rt.cluster_id, rt.region
        from ..models import Agent

        agent = s.get(Agent, rt.agent_id)
        template_id = agent.template_id if agent else None
        tr = s.get(Dataset, body.train_dataset_id)
        if not tr or tr.status != "ready":
            raise AppError("run.bad_dataset", "training dataset is not ready")
        if tr.region != region:
            raise AppError("run.region_mismatch", "dataset and cluster must be in the same region")
        if body.val_dataset_id:
            va = s.get(Dataset, body.val_dataset_id)
            if not va or va.status != "ready" or body.val_split not in (va.splits or {}):
                raise AppError("run.bad_dataset", f"validation split {body.val_split} not found")
    m = models.check_model(body.model_id)
    if not m.compatible:
        raise AppError("run.model_incompatible", m.reason or "model not supported by the gateway")
    params = merge_params(body.params)
    spec = instances.spec(body.compute.instance_type)
    p = make_plan(
        params_b=m.params_b or 0,
        spec=spec,
        nodes=body.compute.nodes,
        max_model_len=params["max_model_len"],
        is_moe=m.is_moe,
        prefer=body.prefer,
        tuning=body.tuning,
    ).to_dict()  # type: ignore[arg-type]
    for k, v in body.plan_overrides.items():
        if k not in p or k in {"warnings", "reasons", "world_size"}:
            raise AppError("run.bad_plan_override", f"cannot override plan field {k}")
        p[k] = v
    if template_id:
        get_template(template_id)
    plan_info = None
    if body.compute.provider == "ec2" and body.compute.capacity == "training_plan":
        raise AppError("nodegroup.bad_capacity", "EC2 node groups use On-Demand or Spot capacity")
    if body.compute.capacity == "training_plan":
        if not body.compute.training_plan_arn:
            raise AppError("plan.required", "pick or enter a training plan")
        c = pc.load(cluster_id)
        plan_info = hp.check_plan(
            region,
            body.compute.training_plan_arn.strip(),
            body.compute.instance_type,
            body.compute.nodes,
            (c.get("network") or {}).get("az_ids") or [],
        )
    else:
        body.compute.training_plan_arn = None
    return {
        "training_plan": plan_info,
        "cluster_id": cluster_id,
        "region": region,
        "plan": p,
        "params": params,
        "model": m.to_dict(),
    }


@router.get("/defaults")
def defaults():
    return {"params": DEFAULT_PARAMS, "agent_loop": AGENT_LOOP_DEFAULTS}


@router.post("/preview")
def preview(body: CreateRun):
    v = _validate(body)
    return {
        "plan": v["plan"],
        "params": v["params"],
        "model": v["model"],
        "estimate": _estimate(v["region"], body.compute, v["params"]),
        "training_plan": v["training_plan"],
    }


@router.post("")
def create(body: CreateRun):
    v = _validate(body)
    est = _estimate(v["region"], body.compute, v["params"])
    if not body.confirm_cost:
        raise AppError(
            "run.confirm_cost", "confirm the estimated GPU cost to start the run", detail=est
        )
    proj.require_region(v["region"])
    rid = new_id("run")
    with session_scope() as s:
        s.add(
            Run(
                id=rid,
                name=body.name,
                agent_runtime_id=body.agent_runtime_id,
                cluster_id=v["cluster_id"],
                train_dataset_id=body.train_dataset_id,
                val_dataset_id=body.val_dataset_id,
                model_id=body.model_id,
                spec={
                    "plan": v["plan"],
                    "params": body.params,
                    "val_split": body.val_split,
                    "prefer": body.prefer,
                    "tuning": body.tuning,
                    "estimate": est,
                },
                compute=body.compute.model_dump(),
                status="queued",
                progress={},
            )
        )
    return {"id": rid, "job_id": get_engine().start("run.train", rid, {})}


def _view(r: Run, metrics: bool = False) -> dict[str, Any]:
    j = latest_job(r.id, "run.train")
    out = {c.name: getattr(r, c.name) for c in Run.__table__.columns}
    prog = dict(out.get("progress") or {})
    prog.pop("log_carry", None)
    if not metrics:
        prog.pop("hydra", None)
    out["progress"] = prog
    out["job"] = job_view(j) if j else None
    return out


@router.get("")
def list_runs():
    with session_scope() as s:
        rows = s.query(Run).order_by(Run.created_at.desc()).all()
        out = []
        for r in rows:
            v = _view(r)
            ms = {}
            for m in s.query(RunMetric).filter(
                RunMetric.run_id == r.id, RunMetric.key.in_(KEY_METRICS)
            ):
                ms.setdefault(m.step, {})[m.key] = m.value
            v["summary"] = summarize(ms)
            out.append(v)
        return out


@router.get("/{run_id}")
def get_run(run_id: str):
    with session_scope() as s:
        r = s.get(Run, run_id)
        if not r:
            raise NotFound("run.not_found", f"run {run_id} not found")
        v = _view(r, metrics=True)
        ms: dict[int, dict[str, float]] = {}
        for m in s.query(RunMetric).filter(RunMetric.run_id == run_id):
            ms.setdefault(m.step, {})[m.key] = m.value
    v["summary"] = summarize(ms)
    v["series"] = {k: [[s, d[k]] for s, d in sorted(ms.items()) if k in d] for k in KEY_METRICS}
    v["metric_keys"] = sorted({k for d in ms.values() for k in d})
    return v


@router.get("/{run_id}/series")
def series(run_id: str, keys: str):
    want = [k for k in keys.split(",") if k][:12]
    with session_scope() as s:
        rows = s.query(RunMetric).filter(RunMetric.run_id == run_id, RunMetric.key.in_(want))
        out: dict[str, list] = {k: [] for k in want}
        for m in rows.order_by(RunMetric.step):
            out[m.key].append([m.step, m.value])
    return out


@router.get("/{run_id}/log")
def log(run_id: str, offset: int = 0):
    run = load_run(run_id)
    region = pc.load(run["cluster_id"])["region"]
    bucket = proj.require_region(region)["bucket"]
    page = 256 * 1024
    text, nxt = svc.read_log(region, bucket, run_id, max(0, offset), max_bytes=page)
    # eof by bytes, not characters: a full page of multi-byte text decodes to < page chars
    return {"content": text, "next_offset": nxt, "eof": nxt - max(0, offset) < page - 3}


@router.get("/{run_id}/pods")
def pods(run_id: str):
    run = load_run(run_id)
    c = pc.load(run["cluster_id"])
    return svc.run_pods(c["region"], c["eks_name"], run_id)


@router.get("/{run_id}/checkpoints")
def checkpoints(run_id: str):
    run = load_run(run_id)
    region = pc.load(run["cluster_id"])["region"]
    bucket = proj.require_region(region)["bucket"]
    return {
        "steps": svc.ckpt_steps(region, bucket, run_id),
        "fsx_dir": f"{svc.run_dir(run_id)}/ckpt",
    }


@router.post("/{run_id}/stop")
def stop(run_id: str):
    j = latest_job(run_id, "run.train")
    if not j or j.status not in ("queued", "running"):
        raise AppError("run.not_active", "run is not active")
    get_engine().cancel(j.id)
    return {"ok": True}


@router.post("/{run_id}/resume")
def resume(run_id: str):
    run = load_run(run_id)
    if run["status"] in ACTIVE:
        raise AppError("run.active", "run is still active", status=409)
    from ..pipelines.run import save_run

    save_run(run_id, status="queued", error=None, ended_at=None, retries=0)
    return {"job_id": get_engine().start("run.train", run_id, {"resume": True})}


@router.delete("/{run_id}")
def delete(run_id: str):
    run = load_run(run_id)
    if run["status"] in ACTIVE:
        raise AppError("run.active", "stop the run first", status=409)
    with session_scope() as s:
        s.query(RunMetric).filter(RunMetric.run_id == run_id).delete()
        r = s.get(Run, run_id)
        if r:
            s.delete(r)
    return {"ok": True}


# ---------------- trainer images ----------------

images = APIRouter(prefix="/api/trainer-images", tags=["trainer-images"])


@images.get("")
def list_images():
    from ..models import TrainerImage

    with session_scope() as s:
        out = []
        for t in s.query(TrainerImage).order_by(TrainerImage.created_at.desc()):
            j = latest_job(t.id, "trainer.build")
            out.append(
                {
                    "id": t.id,
                    "profile": t.profile,
                    "region": t.region,
                    "toolkit_sha": t.toolkit_sha,
                    "image_uri": t.image_uri,
                    "status": t.status,
                    "build_id": t.build_id,
                    "created_at": t.created_at,
                    "job": job_view(j) if j else None,
                }
            )
        return out


class ImageBody(BaseModel):
    region: str | None = None
    profile: str = Field("fsdp", pattern="^(fsdp|megatron)$")


@images.post("")
def build_image(body: ImageBody):
    from ..core.config import get_settings
    from ..pipelines.trainer import find_or_create

    tid, ready = find_or_create(body.region or get_settings().default_region, body.profile)
    return {"id": tid, "ready": ready}
