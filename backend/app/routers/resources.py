from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from fastapi import APIRouter
from pydantic import BaseModel

from ..core import aws
from ..core.config import get_settings
from ..core.db import session_scope
from ..core.errors import AppError
from ..models import (
    Agent,
    AgentRuntime,
    Cluster,
    Dataset,
    Eval,
    Export,
    InferenceEndpoint,
    Run,
    TrainerImage,
)
from ..services import components as comp
from ..services import project as proj

router = APIRouter(prefix="/api", tags=["resources"])
PREFIXES = [
    "datasets/",
    "runs/",
    "rollouts/",
    "exports/",
    "evals/",
    "smoke/",
    "clusters/",
    "build/",
]
PURGEABLE = {"rollouts/", "smoke/", "build/"}
S3_PRICE_GB_MONTH = 0.023


def _prefix_size(region: str, bucket: str, prefix: str, cap: int = 20000) -> dict[str, Any]:
    s3 = aws.client("s3", region)
    total, count = 0, 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            total += o["Size"]
            count += 1
        if count >= cap:
            break
    return {
        "prefix": prefix,
        "objects": count,
        "bytes": total,
        "truncated": count >= cap,
        "monthly_usd": round(total / 1024**3 * S3_PRICE_GB_MONTH, 2),
    }


@router.get("/resources")
def resources(region: str | None = None):
    region = region or get_settings().default_region
    res = proj.region_resources(region)
    out: dict[str, Any] = {"region": region, "project": res}
    with session_scope() as s:
        out["clusters"] = [
            {
                "id": c.id,
                "name": c.name,
                "status": c.status,
                "source": c.source,
                "cfn_stack": c.cfn_stack,
            }
            for c in s.query(Cluster).filter(Cluster.region == region, Cluster.status != "deleted")
        ]
        out["runtimes"] = [
            {
                "id": r.id,
                "agent_id": r.agent_id,
                "runtime_id": r.runtime_id,
                "network_mode": r.network_mode,
                "cluster_id": r.cluster_id,
                "status": r.status,
            }
            for r in s.query(AgentRuntime).filter(AgentRuntime.region == region)
        ]
        out["endpoints"] = [
            {
                "id": e.id,
                "name": e.name,
                "status": e.status,
                "url": e.url,
                "cluster_id": e.cluster_id,
            }
            for e in s.query(InferenceEndpoint).filter(InferenceEndpoint.status != "deleted")
        ]
        out["trainer_images"] = [
            {"id": t.id, "profile": t.profile, "status": t.status, "image_uri": t.image_uri}
            for t in s.query(TrainerImage).filter(TrainerImage.region == region)
        ]
    for c in out["clusters"]:
        try:
            led = comp.read_ledger(region, c["id"]) or {}
            c["gpu_cost_usd"] = round(led.get("total_cost_usd", 0), 2)
        except ClientError:
            c["gpu_cost_usd"] = None
    out["storage"] = []
    out["ecr"] = []
    if res.get("bucket"):
        for p in PREFIXES:
            try:
                out["storage"].append(
                    {**_prefix_size(region, res["bucket"], p), "purgeable": p in PURGEABLE}
                )
            except ClientError as e:
                out["storage"].append({"prefix": p, "error": e.response["Error"]["Code"]})
    for repo in (proj.AGENT_ECR_REPO, proj.TRAINER_ECR_REPO):
        try:
            imgs = (
                aws.client("ecr", region)
                .describe_images(repositoryName=repo)
                .get("imageDetails", [])
            )
            size = sum(i.get("imageSizeInBytes", 0) for i in imgs)
            out["ecr"].append(
                {
                    "repository": repo,
                    "images": len(imgs),
                    "bytes": size,
                    "monthly_usd": round(size / 1024**3 * 0.10, 2),
                }
            )
        except ClientError:
            out["ecr"].append({"repository": repo, "images": 0, "bytes": 0, "monthly_usd": 0})
    return out


class PurgeBody(BaseModel):
    region: str | None = None
    prefix: str


@router.post("/resources/purge")
def purge(body: PurgeBody):
    if body.prefix not in PURGEABLE:
        raise AppError("resources.not_purgeable", f"only {sorted(PURGEABLE)} can be purged here")
    region = body.region or get_settings().default_region
    bucket = proj.require_region(region)["bucket"]
    s3 = aws.client("s3", region)
    deleted = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=body.prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            deleted += len(keys)
    return {"deleted": deleted}


@router.get("/overview")
def overview():
    with session_scope() as s:
        runs = s.query(Run).all()
        active = [
            r
            for r in runs
            if r.status in ("queued", "preparing", "waiting_capacity", "running", "retrying")
        ]
        clusters = s.query(Cluster).filter(Cluster.status != "deleted").all()
        out = {
            "counts": {
                "clusters": len(clusters),
                "agents": s.query(Agent).count(),
                "datasets": s.query(Dataset).filter(Dataset.status == "ready").count(),
                "runs": len(runs),
                "active_runs": len(active),
                "exports": s.query(Export).count(),
                "endpoints": s.query(InferenceEndpoint)
                .filter(InferenceEndpoint.status == "ready")
                .count(),
                "evals": s.query(Eval).count(),
            },
            "run_cost_usd": round(sum(r.est_cost_usd or 0 for r in runs), 2),
            "node_hours": round(sum(r.node_hours or 0 for r in runs), 2),
            "active_runs": [
                {
                    "id": r.id,
                    "name": r.name,
                    "status": r.status,
                    "step": (r.progress or {}).get("step"),
                    "est_cost_usd": r.est_cost_usd,
                }
                for r in active
            ],
            "clusters": [
                {"id": c.id, "name": c.name, "status": c.status, "region": c.region}
                for c in clusters
            ],
        }
    out["setup"] = proj.get_project()["regions"]
    return out
