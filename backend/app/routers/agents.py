from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..core.db import new_id, session_scope
from ..core.errors import AppError, Conflict, NotFound
from ..jobs.engine import get_engine, job_view, latest_job
from ..models import Agent, AgentRuntime, Cluster
from ..pipelines.agent import load_agent, load_runtime
from ..services import agents as svc
from ..templates_lib import get_template, list_templates, resolve_params

router = APIRouter(prefix="/api/agents", tags=["agents"])
templates = APIRouter(prefix="/api/templates", tags=["templates"])

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
MAX_UPLOAD = 100 * 1024**2


@templates.get("")
def get_templates():
    return list_templates()


def _runtime_view(r: AgentRuntime) -> dict[str, Any]:
    j = latest_job(r.id, "agent.deploy")
    return {
        "id": r.id,
        "cluster_id": r.cluster_id,
        "region": r.region,
        "network_mode": r.network_mode,
        "runtime_id": r.runtime_id,
        "runtime_arn": r.runtime_arn,
        "image_uri": r.image_uri,
        "status": r.status,
        "last_smoke": r.last_smoke or {},
        "job": job_view(j) if j else None,
    }


def _view(a: Agent, s) -> dict[str, Any]:
    j = latest_job(a.id)
    rts = s.query(AgentRuntime).filter(AgentRuntime.agent_id == a.id).all()
    return {
        "id": a.id,
        "name": a.name,
        "source": a.source,
        "template_id": a.template_id,
        "contract": a.contract,
        "config": a.config or {},
        "image_uri": a.image_uri,
        "status": a.status,
        "checks": a.checks or {},
        "created_at": a.created_at,
        "job": job_view(j) if j else None,
        "runtimes": [_runtime_view(r) for r in rts],
    }


@router.get("")
def list_agents():
    with session_scope() as s:
        return [_view(a, s) for a in s.query(Agent).order_by(Agent.created_at.desc())]


@router.get("/{agent_id}")
def get_agent(agent_id: str):
    with session_scope() as s:
        a = s.get(Agent, agent_id)
        if not a:
            raise NotFound("agent.not_found", f"agent {agent_id} not found")
        return _view(a, s)


def _validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise AppError("agent.invalid_name", "name: 2-31 lowercase letters, digits or '-'")
    with session_scope() as s:
        if s.query(Agent).filter(Agent.name == name).first():
            raise Conflict("agent.name_taken", f"agent {name} already exists")


class CreateBody(BaseModel):
    name: str
    source: str = Field(pattern="^(template|image)$")
    template_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    image_uri: str | None = None
    smoke_payloads: list[dict[str, Any]] = Field(default_factory=list)
    region: str | None = None


@router.post("")
def create_agent(body: CreateBody):
    _validate_name(body.name)
    region = body.region or get_settings().default_region
    config: dict[str, Any] = {"region": region, "smoke_payloads": body.smoke_payloads}
    if body.source == "template":
        if not body.template_id:
            raise AppError("agent.template_required", "template_id is required")
        get_template(body.template_id)
        config["params"] = resolve_params(body.template_id, body.params)
    else:
        if not body.image_uri:
            raise AppError("agent.image_required", "image_uri is required")
        svc.parse_image_uri(body.image_uri)
    aid = new_id("ag")
    with session_scope() as s:
        s.add(
            Agent(
                id=aid,
                name=body.name,
                source=body.source,
                template_id=body.template_id,
                config=config,
                image_uri=body.image_uri,
                status="queued",
                checks={},
            )
        )
    job_type = "agent.build" if body.source == "template" else "agent.import"
    return {"id": aid, "job_id": get_engine().start(job_type, aid, {"region": region})}


@router.post("/upload")
async def upload_agent(
    name: str = Form(...),
    file: UploadFile = File(...),
    smoke_payloads: str = Form("[]"),
    region: str | None = Form(None),
):
    _validate_name(name)
    try:
        payloads = json.loads(smoke_payloads or "[]")
        assert isinstance(payloads, list) and all(isinstance(p, dict) for p in payloads)
    except Exception as e:
        raise AppError(
            "agent.bad_smoke_payloads", "smoke payloads must be a JSON array of objects"
        ) from e
    if not (file.filename or "").endswith(".zip"):
        raise AppError("agent.bad_zip", "upload a .zip of the agent folder (with a Dockerfile)")
    aid = new_id("ag")
    dest = svc.uploads_dir() / f"{aid}.zip"
    size = 0
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                dest.unlink(missing_ok=True)
                raise AppError("agent.bad_zip", "zip larger than 100 MB")
            f.write(chunk)
    region = region or get_settings().default_region
    with session_scope() as s:
        s.add(
            Agent(
                id=aid,
                name=name,
                source="upload",
                config={"region": region, "smoke_payloads": payloads, "filename": file.filename},
                status="queued",
                checks={},
            )
        )
    return {"id": aid, "job_id": get_engine().start("agent.build", aid, {"region": region})}


@router.post("/{agent_id}/rebuild")
def rebuild(agent_id: str):
    a = load_agent(agent_id)
    job_type = "agent.import" if a["source"] == "image" else "agent.build"
    return {
        "job_id": get_engine().start(
            job_type,
            agent_id,
            {"region": a["config"].get("region") or get_settings().default_region},
        )
    }


class DeployBody(BaseModel):
    cluster_id: str | None = None  # None → standalone PUBLIC smoke runtime
    smoke_payloads: list[dict[str, Any]] | None = None
    skip_smoke: bool = False


@router.post("/{agent_id}/runtimes")
def deploy(agent_id: str, body: DeployBody):
    a = load_agent(agent_id)
    with session_scope() as s:
        if body.cluster_id:
            c = s.get(Cluster, body.cluster_id)
            if not c:
                raise NotFound("cluster.not_found", "cluster not found")
            region, mode = c.region, "VPC"
        else:
            region, mode = a["config"].get("region") or get_settings().default_region, "PUBLIC"
        existing = (
            s.query(AgentRuntime)
            .filter(
                AgentRuntime.agent_id == agent_id,
                AgentRuntime.cluster_id.is_(None)
                if body.cluster_id is None
                else AgentRuntime.cluster_id == body.cluster_id,
            )
            .first()
        )
        if existing:
            rt_id = existing.id
            existing.status = "queued"
        else:
            rt_id = new_id("rt")
            s.add(
                AgentRuntime(
                    id=rt_id,
                    agent_id=agent_id,
                    cluster_id=body.cluster_id,
                    region=region,
                    network_mode=mode,
                    status="queued",
                )
            )
    payload = {"smoke_payloads": body.smoke_payloads, "skip_smoke": body.skip_smoke}
    return {"id": rt_id, "job_id": get_engine().start("agent.deploy", rt_id, payload)}


@router.delete("/{agent_id}/runtimes/{rt_id}")
def delete_runtime(agent_id: str, rt_id: str):
    rt = load_runtime(rt_id)
    if rt["agent_id"] != agent_id:
        raise NotFound("runtime.not_found", "runtime not found")
    if rt["runtime_id"]:
        svc.delete_runtime(rt["region"], rt["runtime_id"])
    with session_scope() as s:
        r = s.get(AgentRuntime, rt_id)
        if r:
            s.delete(r)
    return {"ok": True}


@router.delete("/{agent_id}")
def delete_agent(agent_id: str):
    with session_scope() as s:
        rts = s.query(AgentRuntime).filter(AgentRuntime.agent_id == agent_id).all()
        ids = [(r.region, r.runtime_id) for r in rts if r.runtime_id]
    for region, rid in ids:
        svc.delete_runtime(region, rid)
    with session_scope() as s:
        for r in s.query(AgentRuntime).filter(AgentRuntime.agent_id == agent_id):
            s.delete(r)
        a = s.get(Agent, agent_id)
        if a:
            s.delete(a)
    return {"ok": True}
