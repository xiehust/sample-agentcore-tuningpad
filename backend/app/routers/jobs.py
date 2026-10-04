from __future__ import annotations

from fastapi import APIRouter

from ..core.db import session_scope
from ..core.errors import NotFound
from ..jobs.engine import get_engine, job_view, read_log
from ..models import Job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
def list_jobs(target_id: str | None = None, type: str | None = None, limit: int = 50):
    with session_scope() as s:
        q = s.query(Job)
        if target_id:
            q = q.filter(Job.target_id == target_id)
        if type:
            q = q.filter(Job.type == type)
        return [job_view(j) for j in q.order_by(Job.created_at.desc()).limit(min(limit, 200))]


@router.get("/{job_id}")
def get_job(job_id: str):
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job:
            raise NotFound("job.not_found", f"job {job_id} not found")
        return job_view(job)


@router.get("/{job_id}/log")
def get_log(job_id: str, offset: int = 0):
    return read_log(job_id, offset)


@router.post("/{job_id}/cancel")
def cancel(job_id: str):
    get_engine().cancel(job_id)
    return {"ok": True}


@router.post("/{job_id}/retry")
def retry(job_id: str):
    get_engine().retry(job_id)
    return {"ok": True}
