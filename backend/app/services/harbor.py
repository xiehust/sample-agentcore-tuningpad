"""Harbor-format Tasks and the built-in SmolDataEnvs dataset.

A Harbor Task is a directory with `instruction.md`, `task.toml` (environment image, env,
Verifier settings) and `tests/` (the Verifier). TuningPad accepts only Task sets that share
one environment image (docs/adr/0001): the environment is rebuilt into the Agent image, so
only the instruction, the Verifier and the input files travel through S3.

SmolDataEnvs keeps its input files in a Hugging Face Bucket, one prefix per Kaggle dataset
and shared by many Tasks. Each prefix is stored once per TuningPad dataset:

    s3://<bucket>/datasets/<id>/smoldataenvs/tasks/<task>/{instruction.md,task.toml,tests/}
    s3://<bucket>/datasets/<id>/smoldataenvs/inputs/<prefix>/...
"""

from __future__ import annotations

import random
import tempfile
import tomllib
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pandas as pd

from ..core import aws
from ..core.errors import AppError

SMOL_TRAIN = "FineEnvs/SmolDataEnvs-harbor-train"
SMOL_TEST = "FineEnvs/SmolDataEnvs-harbor-test"
# Pinned dataset revisions: the same import always selects the same Tasks.
SMOL_REVISIONS = {
    SMOL_TRAIN: "588744be728257abfb8d4984ec8fab2acbd1f7e1",
    SMOL_TEST: "607665fff01d8fd80936470e57b73d8528753532",
}
SMOL_BUCKET = "AdithyaSK/jupyter-agent-kaggle-all"
DEFAULT_TRAIN_TASKS = 1000
DEFAULT_MAX_INPUT_BYTES = 1 << 30
# Files the Agent needs; the environment/ directory is baked into the Agent image.
TASK_FILES = ("instruction.md", "task.toml", "tests/*")


class HfHub:
    """The Hugging Face boundary (tests replace it)."""

    def manifest(self, repo: str, revision: str) -> pd.DataFrame:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo, "manifest.parquet", repo_type="dataset", revision=revision)
        return pd.read_parquet(path)

    def prefix_sizes(self, prefixes: Iterable[str]) -> dict[str, int]:
        from huggingface_hub import list_bucket_tree

        out = {}
        for p in prefixes:
            items = list_bucket_tree(SMOL_BUCKET, prefix=f"{p}/", recursive=True)
            out[p] = sum(it.size for it in items if getattr(it, "type", None) == "file")
        return out

    def task_dirs(self, repo: str, revision: str, names: Iterable[str], dest: Path) -> Path:
        from huggingface_hub import snapshot_download

        patterns = [f"tasks/{n}/{f}" for n in names for f in TASK_FILES]
        snapshot_download(
            repo, repo_type="dataset", revision=revision, local_dir=dest, allow_patterns=patterns
        )
        return dest

    def download_prefix(self, prefix: str, dest: Path) -> None:
        from huggingface_hub import download_bucket_files, list_bucket_tree

        items = list_bucket_tree(SMOL_BUCKET, prefix=f"{prefix}/", recursive=True)
        files = [
            (it.path, str(dest / Path(it.path).relative_to(prefix)))
            for it in items
            if getattr(it, "type", None) == "file"
        ]
        dest.mkdir(parents=True, exist_ok=True)
        download_bucket_files(SMOL_BUCKET, files=files, raise_on_missing_files=True)


def _hub() -> HfHub:
    return HfHub()


# ---------------- Harbor Tasks ----------------


def read_task(task_dir: Path) -> dict[str, Any]:
    """The parts of a Harbor Task TuningPad uses."""
    try:
        cfg = tomllib.loads((task_dir / "task.toml").read_text())
        instruction = (task_dir / "instruction.md").read_text()
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise AppError(
            "dataset.harbor_invalid_task", f"{task_dir.name}: not a Harbor task ({e})"
        ) from e
    env = cfg.get("environment") or {}
    return {
        "name": task_dir.name,
        "instruction": instruction,
        "docker_image": env.get("docker_image"),
        "env": env.get("env") or {},
    }


def require_shared_image(tasks: Iterable[dict[str, Any]]) -> str:
    """All Tasks must name one environment image (docs/adr/0001)."""
    images = {t["docker_image"] for t in tasks}
    if len(images) != 1 or None in images:
        shown = ", ".join(sorted(str(i) for i in images)[:3])
        raise AppError(
            "dataset.harbor_mixed_images",
            f"Tasks must share one environment image; found {len(images)}: {shown}",
        )
    return images.pop()


# ---------------- SmolDataEnvs selection ----------------


def _prefix(kaggle: str) -> str:
    return kaggle.replace("/", "__")


def _stratified(rows: list[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """n rows, allocated to difficulty tiers in proportion (largest remainder)."""
    if n >= len(rows):
        return rows
    by_tier: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_tier[r["difficulty_tier"]].append(r)
    quotas = {t: n * len(v) / len(rows) for t, v in by_tier.items()}
    take = {t: int(q) for t, q in quotas.items()}
    for t in sorted(quotas, key=lambda t: (take[t] - quotas[t], t))[: n - sum(take.values())]:
        take[t] += 1
    rng = random.Random(seed)
    picked: set[str] = set()
    for t in sorted(by_tier):
        names = sorted(r["task_dir"] for r in by_tier[t])
        rng.shuffle(names)
        picked.update(names[: take[t]])
    return [r for r in rows if r["task_dir"] in picked]


def _plan(train_n: int, all_tasks: bool, max_input_bytes: int, seed: int) -> dict[str, Any]:
    hub = _hub()
    chosen = {}
    for repo in (SMOL_TRAIN, SMOL_TEST):
        rows = hub.manifest(repo, SMOL_REVISIONS[repo]).to_dict("records")
        if repo == SMOL_TRAIN and not all_tasks:
            rows = _stratified(rows, train_n, seed)
        chosen[repo] = rows
    sizes = hub.prefix_sizes(
        sorted({_prefix(r["kaggle"]) for rows in chosen.values() for r in rows})
    )
    skipped = 0
    for repo, rows in chosen.items():
        kept = [r for r in rows if sizes[_prefix(r["kaggle"])] <= max_input_bytes]
        skipped += len(rows) - len(kept)
        chosen[repo] = kept
    used = {_prefix(r["kaggle"]) for rows in chosen.values() for r in rows}
    return {
        "tasks": chosen,
        "skipped": skipped,
        "input_bytes": sum(sizes[p] for p in used),
    }


def smoldataenvs_estimate(
    *,
    train_n: int = DEFAULT_TRAIN_TASKS,
    all_tasks: bool = False,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    seed: int = 0,
) -> dict[str, int]:
    """What an import with these settings selects and transfers; uploads nothing."""
    plan = _plan(train_n, all_tasks, max_input_bytes, seed)
    return {
        "train": len(plan["tasks"][SMOL_TRAIN]),
        "test": len(plan["tasks"][SMOL_TEST]),
        "skipped": plan["skipped"],
        "input_bytes": plan["input_bytes"],
    }


# ---------------- SmolDataEnvs import ----------------


def _existing(s3, bucket: str, prefix: str) -> dict[str, int]:
    out: dict[str, int] = {}
    kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = s3.list_objects_v2(**kw)
        out.update({o["Key"]: o["Size"] for o in resp.get("Contents", [])})
        if not resp.get("IsTruncated"):
            return out
        kw["ContinuationToken"] = resp["NextContinuationToken"]


def _upload_tree(s3, local: Path, bucket: str, prefix: str) -> int:
    """Upload every file under `local` to `prefix`, skipping objects already there with the
    same size. Returns the number of files uploaded."""
    have = _existing(s3, bucket, prefix)
    sent = 0
    for f in sorted(p for p in local.rglob("*") if p.is_file() and not p.is_symlink()):
        key = f"{prefix}{f.relative_to(local).as_posix()}"
        if "__pycache__" in key or have.get(key) == f.stat().st_size:
            continue
        s3.upload_file(str(f), bucket, key)
        sent += 1
    return sent


def smoldataenvs_rows(
    dataset_id: str,
    region: str,
    bucket: str,
    log: Callable[[str], Any],
    *,
    train_n: int = DEFAULT_TRAIN_TASKS,
    all_tasks: bool = False,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    seed: int = 0,
) -> dict[str, Any]:
    """Stage the selected Tasks and their input files in S3 and return payload rows:
    train, test (held out) and val (= test). Idempotent: a re-run uploads only what is
    missing, and input prefixes already in S3 are not downloaded again."""
    plan = _plan(train_n, all_tasks, max_input_bytes, seed)
    if plan["skipped"]:
        log(f"skipped {plan['skipped']} tasks whose input files exceed {max_input_bytes} bytes")
    hub = _hub()
    base = f"datasets/{dataset_id}/smoldataenvs"
    s3 = aws.client("s3", region)
    out: dict[str, Any] = {"skipped": plan["skipped"]}
    with tempfile.TemporaryDirectory(prefix="smoldataenvs-") as tmp:
        staged: dict[str, list[tuple[dict[str, Any], Path]]] = {}
        for repo, rows in plan["tasks"].items():
            root = hub.task_dirs(
                repo, SMOL_REVISIONS[repo], [r["task_dir"] for r in rows], Path(tmp) / repo
            )
            staged[repo] = [(read_task(root / "tasks" / r["task_dir"]), root) for r in rows]
        image = require_shared_image(t for items in staged.values() for t, _ in items)
        log(f"environment image: {image}")
        for repo, items in staged.items():
            want = {r["task_dir"]: _prefix(r["kaggle"]) for r in plan["tasks"][repo]}
            for t, _ in items:
                if t["env"].get("HF_BUCKET") != SMOL_BUCKET or (
                    t["env"].get("BUCKET_PREFIX") != want[t["name"]]
                ):
                    raise AppError(
                        "dataset.harbor_invalid_task",
                        f"{t['name']}: input files are not where the manifest says",
                    )

        prefixes = sorted(
            {t["env"].get("BUCKET_PREFIX") for items in staged.values() for t, _ in items}
        )
        for n, p in enumerate(prefixes, 1):
            key = f"{base}/inputs/{p}/"
            if _existing(s3, bucket, key):
                continue
            local = Path(tmp) / "inputs" / p
            hub.download_prefix(p, local)
            _upload_tree(s3, local, bucket, key)
            for f in sorted(local.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
            log(f"inputs {n}/{len(prefixes)}: {p}")

        for repo, items in staged.items():
            rows = []
            for task, root in items:
                key = f"{base}/tasks/{task['name']}/"
                task_dir = root / "tasks" / task["name"]
                for sub in ("instruction.md", "task.toml"):
                    if (task_dir / sub).is_file():
                        _upload_one(s3, task_dir / sub, bucket, f"{key}{sub}")
                _upload_tree(s3, task_dir / "tests", bucket, f"{key}tests/")
                rows.append(
                    {
                        "payload": {
                            "task_id": task["name"],
                            "task_uri": f"s3://{bucket}/{key}",
                            "input_uri": (
                                f"s3://{bucket}/{base}/inputs/{task['env']['BUCKET_PREFIX']}/"
                            ),
                        },
                        "prompt": [{"role": "user", "content": task["instruction"]}],
                    }
                )
            out["train" if repo == SMOL_TRAIN else "test"] = rows
            log(f"{'train' if repo == SMOL_TRAIN else 'test'}: {len(rows)} tasks staged")
    out["val"] = out["test"]
    return out


def _upload_one(s3, local: Path, bucket: str, key: str) -> None:
    if _existing(s3, bucket, key).get(key) != local.stat().st_size:
        s3.upload_file(str(local), bucket, key)
