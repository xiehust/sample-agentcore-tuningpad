from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.config import get_settings
from ..jobs.engine import get_engine, job_view, latest_job
from ..services import project as proj

router = APIRouter(prefix="/api/setup", tags=["setup"])


class RegionBody(BaseModel):
    region: str | None = None


def _region(body: RegionBody | None) -> str:
    return (body.region if body and body.region else None) or get_settings().default_region


@router.get("")
def get_setup():
    p = proj.get_project()
    jobs = {}
    for region in p["regions"]:
        j = latest_job(region, "setup.region")
        if j:
            jobs[region] = job_view(j)
    return {**p, "jobs": jobs}


@router.post("/preflight")
def run_preflight(body: RegionBody):
    return proj.preflight(_region(body))


@router.post("/region")
def setup_region(body: RegionBody):
    region = _region(body)
    proj.save_region(region, {"status": "provisioning"})
    job_id = get_engine().start("setup.region", region, {"region": region})
    return {"job_id": job_id, "region": region}
