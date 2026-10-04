"""Datasets: upload → validate → payload parquet → S3; built-in GSM8K / OfficeBench.

verl contract (backends/verl/dataset.py `PayloadDataset`): one `payload` column
with the exact agent invoke payload; the chat `prompt` column is synthesized from
`payload[<prompt_field>]` unless the dataset carries an explicit `prompt` column
(OfficeBench: payload holds S3 URIs, prompt is the task instruction).
"""

from __future__ import annotations

import importlib.util
import io
import json
import random
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from ..core import aws
from ..core.config import get_settings
from ..core.errors import AppError

MAX_ROWS = 2_000_000
SAMPLE_ROWS = 20
GSM8K_REPO = "openai/gsm8k"
OFFICEBENCH_GIT = "https://github.com/zlwang-cs/OfficeBench.git"
_ANSWER_RE = re.compile(r"####\s*(.+?)\s*$")


def local_dir(dataset_id: str) -> Path:
    d = get_settings().data_dir / "datasets" / dataset_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def s3_key(dataset_id: str, split: str) -> str:
    return f"datasets/{dataset_id}/{split}.parquet"


# ---------------- parsing ----------------


def read_rows(path: Path, filename: str) -> list[dict[str, Any]]:
    """Rows as {"payload": {...}, ["prompt": [...]]} from JSONL / JSON / CSV / parquet."""
    name = filename.lower()
    if name.endswith((".jsonl", ".ndjson")):
        raw = []
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise AppError("dataset.bad_json", f"line {i}: {e.msg}") from e
    elif name.endswith(".json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise AppError("dataset.bad_json", "a .json upload must be an array of objects")
        raw = data
    elif name.endswith(".csv"):
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        raw = df.to_dict(orient="records")
    elif name.endswith(".parquet"):
        df = pd.read_parquet(path)
        raw = df.to_dict(orient="records")
    else:
        raise AppError("dataset.bad_format", "supported formats: .jsonl, .json, .csv, .parquet")
    if len(raw) > MAX_ROWS:
        raise AppError("dataset.too_large", f"more than {MAX_ROWS} rows")
    rows = []
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            raise AppError("dataset.bad_row", f"row {i} is not an object")
        if "payload" in r:
            payload = r["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            row: dict[str, Any] = {"payload": _to_jsonable(payload)}
            if r.get("prompt") is not None:
                row["prompt"] = _to_jsonable(r["prompt"])
        else:
            row = {"payload": _to_jsonable(r)}
        rows.append(row)
    return rows


def _to_jsonable(v: Any) -> Any:
    """numpy / pandas scalars and arrays (from parquet) → plain JSON types."""
    if hasattr(v, "tolist"):
        v = v.tolist()
    if isinstance(v, dict):
        return {str(k): _to_jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_to_jsonable(x) for x in v]
    return v


def validate_rows(
    rows: list[dict[str, Any]],
    *,
    prompt_field: str | None,
    required: list[str] | None = None,
    explicit_prompt: bool = False,
) -> dict[str, Any]:
    if not rows:
        raise AppError("dataset.empty", "dataset has no rows")
    errors: list[str] = []
    field_counts: dict[str, int] = defaultdict(int)
    for i, row in enumerate(rows):
        p = row["payload"]
        if not isinstance(p, dict):
            errors.append(f"row {i}: payload must be an object")
            continue
        if "_rollout" in p or "_config" in p:
            errors.append(f"row {i}: payload must not contain reserved key _rollout/_config")
        for k in p:
            field_counts[k] += 1
        for k in required or []:
            if k not in p or p[k] in (None, ""):
                errors.append(f"row {i}: missing required field '{k}'")
        if explicit_prompt:
            pr = row.get("prompt")
            if not (isinstance(pr, list) and pr and isinstance(pr[0], dict) and "content" in pr[0]):
                errors.append(f"row {i}: explicit chat 'prompt' column required")
        elif prompt_field and not isinstance(p.get(prompt_field), str):
            errors.append(f"row {i}: payload['{prompt_field}'] must be a string")
        try:
            json.dumps(p)
        except (TypeError, ValueError):
            errors.append(f"row {i}: payload is not JSON-serializable")
        if len(errors) >= 20:
            break
    if errors:
        raise AppError(
            "dataset.invalid",
            f"{len(errors)} problem(s), first: {errors[0]}",
            detail={"errors": errors},
        )
    return {"rows": len(rows), "fields": dict(field_counts)}


def split_rows(rows: list[dict[str, Any]], val_fraction: float, seed: int = 0):
    idx = list(range(len(rows)))
    random.Random(seed).shuffle(idx)
    n_val = max(1, round(len(rows) * val_fraction)) if val_fraction > 0 else 0
    val = [rows[i] for i in sorted(idx[:n_val])]
    train = [rows[i] for i in sorted(idx[n_val:])]
    return train, val


def write_split(
    dataset_id: str, split: str, rows: list[dict[str, Any]], region: str, bucket: str
) -> dict[str, Any]:
    cols = ["payload"] + (["prompt"] if any("prompt" in r for r in rows) else [])
    df = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols)
    path = local_dir(dataset_id) / f"{split}.parquet"
    df.to_parquet(path, index=False)
    key = s3_key(dataset_id, split)
    aws.client("s3", region).upload_file(str(path), bucket, key)
    return {"s3_key": key, "s3_uri": f"s3://{bucket}/{key}", "rows": len(rows)}


# ---------------- built-ins ----------------


def gsm8k_rows() -> dict[str, list[dict[str, Any]]]:
    from huggingface_hub import hf_hub_download

    out = {}
    for split in ("train", "test"):
        path = hf_hub_download(
            GSM8K_REPO, f"main/{split}-00000-of-00001.parquet", repo_type="dataset"
        )
        df = pd.read_parquet(path)
        rows = []
        for q, a in zip(df["question"], df["answer"], strict=True):
            m = _ANSWER_RE.search(a)
            if not m:
                continue
            rows.append({"payload": {"prompt": q, "answer": m.group(1).replace(",", "").strip()}})
        out[split] = rows
    return out


def _toolkit_module(rel: str, name: str):
    path = get_settings().toolkit_path / rel
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AppError("dataset.toolkit_missing", f"{path} not found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def officebench_checkout(ref: str = "main") -> Path:
    dest = get_settings().data_dir / "cache" / "OfficeBench"
    if not (dest / "tasks").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", ref, OFFICEBENCH_GIT, str(dest)],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if proc.returncode != 0:
            raise AppError(
                "dataset.git_failed", "git clone OfficeBench failed", detail=proc.stderr[-2000:]
            )
    return dest


def officebench_rows(
    dataset_id: str,
    region: str,
    bucket: str,
    log,
    *,
    val_fraction: float = 0.2,
    seed: int = 0,
    limit: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Upload tasks + testbeds to the platform bucket (toolkit preprocess.upload_task),
    then split by task directory, stratified by app-count category (1/2/3)."""
    repo = officebench_checkout()
    pre = _toolkit_module("examples/strands_officebench_agent/preprocess.py", "ob_preprocess")
    tasks_root = repo / "tasks"
    task_ids = sorted(
        (p.name for p in tasks_root.iterdir() if p.is_dir()),
        key=lambda t: tuple(int(x) if x.isdigit() else 999 for x in t.split("-")),
    )
    if limit:
        task_ids = task_ids[:limit]
    prefix = f"datasets/{dataset_id}/officebench"
    s3 = aws.client("s3", region)
    entries = []
    for n, task_id in enumerate(task_ids, 1):
        count = pre.upload_task(s3, str(tasks_root / task_id), task_id, bucket, prefix)
        sub_dir = tasks_root / task_id / "subtasks"
        has_tb = any(
            (tasks_root / task_id / d).is_dir() and any((tasks_root / task_id / d).iterdir())
            for d in ("testbed", "reference")
        )
        for f in sorted(sub_dir.glob("*.json")) if count else []:
            cfg = json.loads(f.read_text())
            task_text = cfg.get("task")
            if not isinstance(task_text, str):
                continue
            entries.append(
                {
                    "task_id": task_id,
                    "payload": {
                        "task_uri": f"s3://{bucket}/{prefix}/{task_id}/{f.stem}/config.json",
                        "testbed_uri": f"s3://{bucket}/{prefix}/{task_id}/testbed.tar.gz"
                        if has_tb
                        else None,
                    },
                    "prompt": [{"role": "user", "content": task_text}],
                }
            )
        if n % 25 == 0:
            log(f"uploaded {n}/{len(task_ids)} task dirs")
    by_cat: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        by_cat[e["task_id"].split("-")[0]].add(e["task_id"])
    rng = random.Random(seed)
    val_tasks: set[str] = set()
    for cat in sorted(by_cat):
        ids = sorted(by_cat[cat])
        rng.shuffle(ids)
        val_tasks.update(ids[: round(len(ids) * val_fraction)])
    out: dict[str, list[dict[str, Any]]] = {"train": [], "val": []}
    for e in entries:
        out["val" if e["task_id"] in val_tasks else "train"].append(
            {"payload": e["payload"], "prompt": e["prompt"]}
        )
    return out


def preview(dataset_id: str, split: str, limit: int = SAMPLE_ROWS) -> list[dict[str, Any]]:
    path = local_dir(dataset_id) / f"{split}.parquet"
    if not path.exists():
        return []
    df = pd.read_parquet(path).head(limit)
    return [{k: _to_jsonable(v) for k, v in r.items()} for r in df.to_dict(orient="records")]


def read_upload_bytes(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)
