"""Dataset pipelines: dataset.ingest (uploaded files) and dataset.builtin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.db import session_scope
from ..core.errors import AppError
from ..jobs.engine import Stage, StageContext, register
from ..models import Dataset
from ..services import datasets as svc
from ..services import project as proj

VAL_SUBSET = 200  # verified experiment: 200-question val subset keeps validation cheap


def _save(dataset_id: str, **fields: Any) -> None:
    with session_scope() as s:
        d = s.get(Dataset, dataset_id)
        if d:
            for k, v in fields.items():
                setattr(d, k, v)


def _load(dataset_id: str) -> dict[str, Any]:
    with session_scope() as s:
        d = s.get(Dataset, dataset_id)
        return {
            "id": d.id,
            "region": d.region,
            "prompt_field": d.prompt_field,
            "has_prompt_column": d.has_prompt_column,
            "template_id": d.template_id,
        }


def _write(ctx: StageContext, splits: dict[str, list[dict]]) -> None:
    d = _load(ctx.target_id)
    res = proj.require_region(d["region"])
    out = {}
    for name, rows in splits.items():
        if rows:
            out[name] = svc.write_split(d["id"], name, rows, d["region"], res["bucket"])
            ctx.log(f"{name}: {len(rows)} rows → {out[name]['s3_uri']}")
    first = splits.get("train") or next(iter(splits.values()))
    _save(d["id"], splits=out, sample=first[: svc.SAMPLE_ROWS], status="ready")


def stage_ingest(ctx: StageContext) -> None:
    d = _load(ctx.target_id)
    p = ctx.payload
    req = p.get("required") or []
    splits: dict[str, list[dict]] = {}
    for split in ("train", "val"):
        f = p.get(f"{split}_file")
        if f:
            rows = svc.read_rows(Path(f), p[f"{split}_name"])
            stats = svc.validate_rows(
                rows,
                prompt_field=d["prompt_field"],
                required=req,
                explicit_prompt=d["has_prompt_column"],
            )
            ctx.log(f"{split}: {stats}")
            splits[split] = rows
    if "train" not in splits:
        raise AppError("dataset.no_train", "a training file is required")
    if "val" not in splits and p.get("val_fraction", 0) > 0:
        splits["train"], splits["val"] = svc.split_rows(splits["train"], p["val_fraction"])
        ctx.log(f"auto-split: {len(splits['train'])} train / {len(splits['val'])} val")
    _save(d["id"], stats={k: len(v) for k, v in splits.items()})
    ctx.set("ready", True)
    _write(ctx, splits)


def stage_builtin(ctx: StageContext) -> None:
    d = _load(ctx.target_id)
    kind = ctx.payload["builtin"]
    if kind == "gsm8k":
        data = svc.gsm8k_rows()
        splits = {"train": data["train"], "val": data["test"][:VAL_SUBSET], "test": data["test"]}
    elif kind == "officebench":
        res = proj.require_region(d["region"])
        data = svc.officebench_rows(
            d["id"], d["region"], res["bucket"], ctx.log, limit=ctx.payload.get("limit")
        )
        splits = {"train": data["train"], "val": data["val"]}
    else:
        raise AppError("dataset.unknown_builtin", f"unknown built-in dataset {kind}")
    for rows in splits.values():
        svc.validate_rows(
            rows, prompt_field=d["prompt_field"], explicit_prompt=d["has_prompt_column"]
        )
    _save(d["id"], stats={k: len(v) for k, v in splits.items()})
    _write(ctx, splits)


def _done(target: str, status: str, error: str | None) -> None:
    if status != "succeeded":
        _save(target, status="failed")


register("dataset.ingest", [Stage("ingest", stage_ingest)], on_finish=_done)
register("dataset.builtin", [Stage("generate", stage_builtin)], on_finish=_done)
