"""Harness assignment for a training run.

A Task names no Harness; a run does. When a run starts, each row of its own copy of the
data (one Task) gets a Harness in its payload, and verl expands the row into `rollout_n`
rollouts, so every GRPO group runs under a single Harness. The assignment is seeded and
written once per run: retries and resumes reuse the same file.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

from ..core import aws
from ..core.errors import AppError

DEFAULT_SEED = 0


def installed(agent_config: dict[str, Any] | None) -> list[str] | None:
    """The Harnesses an Agent image contains (its `harnesses` template param), if any."""
    return ((agent_config or {}).get("params") or {}).get("harnesses")


def choose(installed: list[str] | None, requested: list[str] | None) -> list[str] | None:
    """The run's Harnesses: the requested ones (default: all) of those in the Agent image,
    in the image's order. None for an Agent without Harnesses."""
    if not installed:
        if requested:
            raise AppError("harness.not_installed", "this agent has no harnesses to pick")
        return None
    picked = installed if requested is None else requested
    if not picked or not set(picked) <= set(installed):
        raise AppError(
            "harness.not_installed",
            f"harnesses must be a non-empty subset of the agent's {installed}",
            detail={"installed": installed, "requested": requested},
        )
    return [h for h in installed if h in picked]


def run_spec(names: list[str] | None) -> dict[str, Any] | None:
    """What a run stores as spec.harnesses."""
    return {"names": names, "seed": DEFAULT_SEED} if names else None


def _harness_of(row: dict[str, Any]) -> str | None:
    return (row.get("payload") or {}).get("harness") or None


def assign(rows: list[dict[str, Any]], names: list[str], seed: int) -> list[dict[str, Any]]:
    """Rows without a Harness get one, uniformly at random but balanced (counts differ by
    at most one); rows that already name a Harness keep it."""
    open_rows = [i for i, r in enumerate(rows) if not _harness_of(r)]
    random.Random(seed).shuffle(open_rows)
    harness_of = {i: names[k % len(names)] for k, i in enumerate(open_rows)}
    return [
        {**r, "payload": {**r["payload"], "harness": harness_of[i]}} if i in harness_of else r
        for i, r in enumerate(rows)
    ]


def check_dataset(rows: list[dict[str, Any]], installed: list[str]) -> None:
    named = {_harness_of(r) for r in rows} - {None}
    if not named <= set(installed):
        raise AppError(
            "run.dataset_harness_invalid",
            f"the dataset names harnesses the agent lacks: {sorted(named - set(installed))}",
            detail={"dataset": sorted(named), "installed": installed},
        )


def _bucket_key(uri: str) -> tuple[str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


def read_rows(region: str, uri: str, columns: list[str] | None = None) -> list[dict[str, Any]]:
    """Parquet rows via pyarrow, which keeps nested types (pandas turns optional ints into
    floats)."""
    with tempfile.TemporaryDirectory(prefix="harness-assign-") as d:
        dest = Path(d) / "split.parquet"
        aws.client("s3", region).download_file(*_bucket_key(uri), str(dest))
        return pq.read_table(dest, columns=columns).to_pylist()


def _write_rows(region: str, uri: str, rows: list[dict[str, Any]]) -> None:
    with tempfile.TemporaryDirectory(prefix="harness-assign-") as d:
        src = Path(d) / "split.parquet"
        pq.write_table(pa.Table.from_pylist(rows), src)
        bucket, key = _bucket_key(uri)
        aws.client("s3", region).upload_file(str(src), bucket, key)


def _exists(region: str, uri: str) -> bool:
    bucket, key = _bucket_key(uri)
    try:
        aws.client("s3", region).head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def validate_datasets(region: str, datasets: dict[str, str], installed: list[str]) -> None:
    for uri in datasets.values():
        check_dataset(read_rows(region, uri, columns=["payload"]), installed)


def stage_run_data(
    run_id: str, region: str, bucket: str, datasets: dict[str, str], harnesses: dict[str, Any]
) -> dict[str, str]:
    """The run's own train/val parquet with a Harness per row, and their URIs. Written by
    the first attempt only: later attempts find the files and reuse them, even if the
    source dataset changed since."""
    out = {}
    for split, uri in datasets.items():
        target = f"s3://{bucket}/runs/{run_id}/data/{split}.parquet"
        if not _exists(region, target):
            rows = assign(read_rows(region, uri), harnesses["names"], int(harnesses["seed"]))
            _write_rows(region, target, rows)
        out[split] = target
    return out
