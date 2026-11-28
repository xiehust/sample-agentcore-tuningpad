"""Harness assignment for a training run.

Each Task row gets one Harness in its payload; verl expands a row into `rollout_n`
rollouts, so every GRPO group runs under a single Harness. The assignment is seeded and
stored with the run, so a retry or resume trains on exactly the same rows.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from ..core import aws
from ..core.errors import AppError

DEFAULT_SEED = 0


def choose(installed: list[str] | None, requested: list[str] | None) -> list[str] | None:
    """The run's Harnesses: the requested ones (default: all) of those in the Agent image,
    in the image's order. None for an Agent without Harnesses."""
    if not installed:
        if requested:
            raise AppError("run.harness_not_installed", "this agent has no harnesses to pick")
        return None
    picked = installed if requested is None else requested
    if not picked or not set(picked) <= set(installed):
        raise AppError(
            "run.harness_not_installed",
            f"harnesses must be a non-empty subset of the agent's {installed}",
            detail={"installed": installed, "requested": requested},
        )
    return [h for h in installed if h in picked]


def assign(rows: list[dict[str, Any]], names: list[str], seed: int) -> list[dict[str, Any]]:
    """Rows without a Harness get one, uniformly at random but balanced (counts differ by
    at most one); rows that already name a Harness keep it."""
    open_rows = [i for i, r in enumerate(rows) if not (r.get("payload") or {}).get("harness")]
    random.Random(seed).shuffle(open_rows)
    harness_of = {i: names[k % len(names)] for k, i in enumerate(open_rows)}
    return [
        {**r, "payload": {**r["payload"], "harness": harness_of[i]}} if i in harness_of else r
        for i, r in enumerate(rows)
    ]


def check_dataset(rows: list[dict[str, Any]], names: list[str]) -> None:
    named = {(r.get("payload") or {}).get("harness") for r in rows} - {None, ""}
    if not named <= set(names):
        raise AppError(
            "run.dataset_harness_invalid",
            f"the dataset names harnesses this run does not use: {sorted(named - set(names))}",
            detail={"dataset": sorted(named), "run": names},
        )


def _split(uri: str) -> tuple[str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


def read_rows(region: str, uri: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="harness-assign-") as d:
        dest = Path(d) / "split.parquet"
        bucket, key = _split(uri)
        aws.client("s3", region).download_file(bucket, key, str(dest))
        return pd.read_parquet(dest).to_dict("records")


def _write_rows(region: str, uri: str, rows: list[dict[str, Any]]) -> None:
    with tempfile.TemporaryDirectory(prefix="harness-assign-") as d:
        src = Path(d) / "split.parquet"
        pd.DataFrame(rows).to_parquet(src, index=False)
        bucket, key = _split(uri)
        aws.client("s3", region).upload_file(str(src), bucket, key)


def validate_datasets(region: str, datasets: dict[str, str], names: list[str]) -> None:
    for uri in datasets.values():
        check_dataset(read_rows(region, uri), names)


def stage_run_data(
    run_id: str, region: str, bucket: str, datasets: dict[str, str], harnesses: dict[str, Any]
) -> dict[str, str]:
    """Write the run's own train/val parquet with Harnesses assigned and return their URIs.
    Deterministic, so re-running it (each attempt) rewrites identical data."""
    out = {}
    for split, uri in datasets.items():
        rows = read_rows(region, uri)
        check_dataset(rows, harnesses["names"])
        target = f"s3://{bucket}/runs/{run_id}/data/{split}.parquet"
        _write_rows(region, target, assign(rows, harnesses["names"], int(harnesses["seed"])))
        out[split] = target
    return out
