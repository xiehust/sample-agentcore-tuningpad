from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..core.db import new_id, session_scope
from ..core.errors import AppError, NotFound
from ..jobs.engine import get_engine, job_view, latest_job
from ..models import Dataset
from ..services import datasets as svc
from ..services import project as proj
from ..templates_lib import get_template

router = APIRouter(prefix="/api/datasets", tags=["datasets"])
MAX_UPLOAD = 500 * 1024**2


def _view(d: Dataset) -> dict[str, Any]:
    j = latest_job(d.id)
    return {
        "id": d.id,
        "name": d.name,
        "region": d.region,
        "source": d.source,
        "template_id": d.template_id,
        "status": d.status,
        "splits": d.splits or {},
        "prompt_field": d.prompt_field,
        "has_prompt_column": d.has_prompt_column,
        "stats": d.stats or {},
        "created_at": d.created_at,
        "job": job_view(j) if j else None,
    }


@router.get("")
def list_datasets():
    with session_scope() as s:
        return [_view(d) for d in s.query(Dataset).order_by(Dataset.created_at.desc())]


@router.get("/{dataset_id}")
def get_dataset(dataset_id: str):
    with session_scope() as s:
        d = s.get(Dataset, dataset_id)
        if not d:
            raise NotFound("dataset.not_found", f"dataset {dataset_id} not found")
        return _view(d)


@router.get("/{dataset_id}/preview")
def preview(dataset_id: str, split: str = "train", limit: int = 20):
    return svc.preview(dataset_id, split, min(limit, 100))


class BuiltinBody(BaseModel):
    template_id: str
    name: str | None = None
    region: str | None = None
    limit: int | None = Field(None, ge=1, le=1000)  # OfficeBench: subset of task dirs


@router.post("/builtin")
def builtin(body: BuiltinBody):
    t = get_template(body.template_id)
    kind = (t.get("dataset") or {}).get("builtin")
    if not kind:
        raise AppError("dataset.no_builtin", f"template {body.template_id} has no built-in dataset")
    region = body.region or get_settings().default_region
    proj.require_region(region)
    pay = t.get("payload") or {}
    did = new_id("ds")
    with session_scope() as s:
        s.add(
            Dataset(
                id=did,
                name=body.name or kind,
                region=region,
                source=f"builtin:{kind}",
                template_id=body.template_id,
                status="generating",
                prompt_field=pay.get("prompt_field") or "prompt",
                has_prompt_column=bool(pay.get("explicit_prompt_column")),
                splits={},
                sample=[],
                stats={},
            )
        )
    job = get_engine().start("dataset.builtin", did, {"builtin": kind, "limit": body.limit})
    return {"id": did, "job_id": job}


async def _save_upload(f: UploadFile, dest_dir) -> str:
    dest = dest_dir / (f.filename or "upload").replace("/", "_")
    size = 0
    with dest.open("wb") as out:
        while chunk := await f.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise AppError("dataset.too_large", "file larger than 500 MB")
            out.write(chunk)
    return str(dest)


@router.post("/upload")
async def upload(
    name: str = Form(...),
    train: UploadFile = File(...),
    val: UploadFile | None = File(None),
    prompt_field: str = Form("prompt"),
    explicit_prompt_column: bool = Form(False),
    required_fields: str = Form(""),
    val_fraction: float = Form(0.0),
    template_id: str | None = Form(None),
    region: str | None = Form(None),
):
    region = region or get_settings().default_region
    proj.require_region(region)
    if not 0 <= val_fraction < 0.9:
        raise AppError("dataset.bad_fraction", "val_fraction must be in [0, 0.9)")
    required = [x.strip() for x in required_fields.split(",") if x.strip()]
    if template_id:
        pay = get_template(template_id).get("payload") or {}
        required = sorted(set(required) | set(pay.get("required") or []))
    did = new_id("ds")
    up = svc.local_dir(did) / "upload"
    up.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "required": required,
        "val_fraction": val_fraction,
        "train_file": await _save_upload(train, up),
        "train_name": train.filename or "",
    }
    if val is not None and val.filename:
        payload["val_file"] = await _save_upload(val, up)
        payload["val_name"] = val.filename
    with session_scope() as s:
        s.add(
            Dataset(
                id=did,
                name=name,
                region=region,
                source="upload",
                template_id=template_id,
                status="validating",
                prompt_field=prompt_field,
                has_prompt_column=explicit_prompt_column,
                splits={},
                sample=[],
                stats={},
            )
        )
    return {"id": did, "job_id": get_engine().start("dataset.ingest", did, payload)}


@router.delete("/{dataset_id}")
def delete(dataset_id: str):
    import shutil

    with session_scope() as s:
        d = s.get(Dataset, dataset_id)
        if not d:
            raise NotFound("dataset.not_found", "dataset not found")
        region = d.region
        s.delete(d)
    bucket = proj.region_resources(region).get("bucket")
    if bucket:
        from ..core import aws

        s3 = aws.client("s3", region)
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"datasets/{dataset_id}/"
        ):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
    shutil.rmtree(svc.local_dir(dataset_id), ignore_errors=True)
    return {"ok": True}
