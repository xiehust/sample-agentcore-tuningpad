from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..core.db import new_id, session_scope
from ..core.errors import AppError, NotFound
from ..jobs.engine import get_engine, job_view, latest_job
from ..models import Eval, Export, InferenceEndpoint, Run
from ..pipelines import cluster as pc
from ..services import evals as esvc

exports = APIRouter(prefix="/api/exports", tags=["exports"])
endpoints = APIRouter(prefix="/api/inference", tags=["inference"])
evals = APIRouter(prefix="/api/evals", tags=["evals"])


def _cols(o) -> dict[str, Any]:
    out = {c.name: getattr(o, c.name) for c in o.__table__.columns}
    j = latest_job(o.id)
    out["job"] = job_view(j) if j else None
    return out


# ---------------- exports ----------------


@exports.get("")
def list_exports():
    with session_scope() as s:
        return [_cols(e) for e in s.query(Export).order_by(Export.created_at.desc())]


class ExportBody(BaseModel):
    run_id: str
    step: int = Field(ge=0)


@exports.post("")
def create_export(body: ExportBody):
    with session_scope() as s:
        r = s.get(Run, body.run_id)
        if not r:
            raise NotFound("run.not_found", "run not found")
        steps = (r.progress or {}).get("ckpt_steps") or []
        if steps and body.step not in steps:
            raise AppError(
                "export.no_checkpoint",
                f"no checkpoint at step {body.step}",
                detail={"steps": steps},
            )
        eid = new_id("ex")
        s.add(Export(id=eid, run_id=body.run_id, step=body.step, status="queued"))
    return {"id": eid, "job_id": get_engine().start("export.merge", eid, {})}


# ---------------- inference ----------------


@endpoints.get("")
def list_endpoints():
    with session_scope() as s:
        return [
            _cols(e)
            for e in s.query(InferenceEndpoint)
            .filter(InferenceEndpoint.status != "deleted")
            .order_by(InferenceEndpoint.created_at.desc())
        ]


class EndpointBody(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    cluster_id: str
    instance_group: str
    source: str = Field(pattern="^(hf|export)$")
    model_id: str | None = None  # hf
    export_id: str | None = None  # export
    tp: int = Field(1, ge=1, le=8)
    replicas: int = Field(1, ge=1, le=8)
    max_model_len: int | None = Field(None, ge=512)


@endpoints.post("")
def create_endpoint(body: EndpointBody):
    pc.load(body.cluster_id)
    src: dict[str, Any] = {"kind": body.source, "max_model_len": body.max_model_len}
    if body.source == "hf":
        if not body.model_id:
            raise AppError("inference.model_required", "model_id is required")
        src.update(model_id=body.model_id, base_model_id=body.model_id)
        served = body.model_id
    else:
        with session_scope() as s:
            ex = s.get(Export, body.export_id or "")
            if not ex:
                raise NotFound("export.not_found", "export not found")
            run = s.get(Run, ex.run_id)
            src.update(export_id=ex.id, base_model_id=run.model_id if run else None)
            served = f"{run.name if run else 'run'}-step{ex.step}"
    eid = new_id("ep")
    with session_scope() as s:
        s.add(
            InferenceEndpoint(
                id=eid,
                name=body.name,
                cluster_id=body.cluster_id,
                model_source=src,
                served_model_name=served,
                instance_group=body.instance_group,
                replicas=body.replicas,
                tp=body.tp,
                status="queued",
            )
        )
    return {"id": eid, "job_id": get_engine().start("inference.deploy", eid, {})}


@endpoints.delete("/{endpoint_id}")
def delete_endpoint(endpoint_id: str):
    with session_scope() as s:
        if not s.get(InferenceEndpoint, endpoint_id):
            raise NotFound("inference.not_found", "endpoint not found")
    return {"job_id": get_engine().start("inference.delete", endpoint_id, {})}


# ---------------- evals ----------------


@evals.get("")
def list_evals():
    with session_scope() as s:
        return [_cols(e) for e in s.query(Eval).order_by(Eval.created_at.desc())]


class EvalBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    endpoint_id: str
    agent_runtime_id: str
    dataset_id: str
    split: str = "val"
    limit: int = Field(200, ge=1, le=20000)
    # per-turn overrides (max_tokens, temperature, top_p, top_k); default: the run's val settings
    sampling_params: dict[str, float | int] | None = None
    # record AgentCore traces (waterfall): runs on the eval-only OTEL twin of the runtime
    observe: bool = False


@evals.post("")
def create_eval(body: EvalBody):
    if body.observe:
        from ..models import AgentRuntime
        from ..services import observability as obs

        with session_scope() as s:
            rt = s.get(AgentRuntime, body.agent_runtime_id)
            if not rt:
                raise NotFound("runtime.not_found", "agent runtime not found")
            region = rt.region
        # fail before creating the eval: the console asks to enable it (account-level)
        if obs.transaction_search_status(region)["transaction_search"] == "off":
            raise AppError(
                "eval.observability_off",
                f"CloudWatch Transaction Search is off in {region}",
                status=409,
                detail={"region": region},
            )
    eid = new_id("ev")
    with session_scope() as s:
        s.add(
            Eval(
                id=eid,
                name=body.name,
                endpoint_id=body.endpoint_id,
                agent_runtime_id=body.agent_runtime_id,
                dataset_id=body.dataset_id,
                split=body.split,
                limit=body.limit,
                status="queued",
                summary={},
                observe=body.observe,
            )
        )
    payload = {"sampling_params": body.sampling_params} if body.sampling_params else {}
    return {"id": eid, "job_id": get_engine().start("eval.run", eid, payload)}


@evals.get("/{eval_id}/samples")
def eval_samples(
    eval_id: str,
    status: str = Query("all", pattern="^(all|failed|truncated)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=esvc.PAGE_MAX),
):
    return esvc.list_samples(eval_id, status, offset, limit)


@evals.get("/{eval_id}/samples/{index}")
def eval_sample(eval_id: str, index: int):
    return esvc.sample_detail(eval_id, index)


@evals.get("/{eval_id}/samples/{index}/trace")
def eval_sample_trace(eval_id: str, index: int, force: bool = False):
    from ..services import observability as obs

    return obs.sample_trace(eval_id, index, force)
