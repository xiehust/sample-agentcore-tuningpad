"""Harbor-format Tasks and the built-in SmolDataEnvs import (HF and S3 stubbed)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.core.errors import AppError
from app.services import harbor
from tests.conftest import StubClient

IMAGE = "docker.io/example/env@sha256:abc"


def task_toml(name: str, prefix: str, image: str = IMAGE) -> str:
    return f"""schema_version = "1.2"
[task]
name = "smoldataenvs/{name}"
[environment]
docker_image = "{image}"
[environment.env]
HF_BUCKET = "{harbor.SMOL_BUCKET}"
BUCKET_PREFIX = "{prefix}"
[verifier]
timeout_sec = 120.0
[verifier.env]
EXPECTED_ANSWER = "42"
"""


class FakeHub:
    """Hugging Face boundary: manifests, Task directories and bucket prefixes."""

    def __init__(self, tasks: dict[str, list[dict]], sizes: dict[str, int], images=None):
        self.tasks = tasks  # repo -> manifest rows
        self.sizes = sizes  # bucket prefix -> total bytes
        self.images = images or {}
        self.downloaded_prefixes: list[str] = []

    def manifest(self, repo: str, revision: str) -> pd.DataFrame:
        return pd.DataFrame(self.tasks[repo])

    def prefix_sizes(self, prefixes):
        return {p: self.sizes[p] for p in prefixes}

    def task_dirs(self, repo: str, revision: str, names, dest: Path) -> Path:
        for row in self.tasks[repo]:
            if row["task_dir"] not in names:
                continue
            d = dest / "tasks" / row["task_dir"]
            (d / "tests").mkdir(parents=True)
            (d / "environment").mkdir()
            (d / "instruction.md").write_text(f"Question for {row['task_dir']}")
            prefix = row["kaggle"].replace("/", "__")
            image = self.images.get(row["task_dir"], IMAGE)
            (d / "task.toml").write_text(task_toml(row["task_dir"], prefix, image))
            (d / "tests" / "test.sh").write_text("#!/bin/sh\n")
            (d / "tests" / "grader.py").write_text("print(1.0)\n")
            (d / "environment" / "Dockerfile").write_text("FROM x\n")
        return dest

    def download_prefix(self, prefix: str, dest: Path) -> None:
        self.downloaded_prefixes.append(prefix)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "data.csv").write_text("a,b\n1,2\n")


def manifest_rows(n: int, tiers: list[str], kaggle_of=lambda i: f"owner/ds{i % 3}"):
    return [
        {
            "task_dir": f"{i:04d}_task",
            "difficulty_tier": tiers[i % len(tiers)],
            "kaggle": kaggle_of(i),
        }
        for i in range(n)
    ]


def s3_stub(existing: dict[str, int] | None = None):
    existing = dict(existing or {})

    def list_objects_v2(Bucket, Prefix, **_):
        hits = [{"Key": k, "Size": s} for k, s in existing.items() if k.startswith(Prefix)]
        return {"Contents": hits, "IsTruncated": False}

    def upload_file(_args):
        local, _bucket, key = _args
        existing[key] = Path(local).stat().st_size

    return StubClient(list_objects_v2=list_objects_v2, upload_file=upload_file), existing


@pytest.fixture
def hub(monkeypatch):
    def install(fake: FakeHub) -> FakeHub:
        monkeypatch.setattr(harbor, "_hub", lambda: fake)
        return fake

    return install


def test_train_subset_is_stratified_by_difficulty_and_repeatable(hub, stub_aws):
    fake = hub(
        FakeHub(
            {
                harbor.SMOL_TRAIN: manifest_rows(40, ["easy", "medium", "medium", "hard"]),
                harbor.SMOL_TEST: manifest_rows(4, ["hard"]),
            },
            sizes={f"owner__ds{i}": 10 for i in range(3)},
        )
    )
    s3, _ = s3_stub()
    stub_aws({"s3": s3})
    out = harbor.smoldataenvs_rows("ds-1", "us-east-1", "bkt", print, train_n=8, seed=7)
    tiers = {r["task_dir"]: r["difficulty_tier"] for r in fake.tasks[harbor.SMOL_TRAIN]}
    picked = [r["payload"]["task_id"] for r in out["train"]]
    assert len(picked) == 8
    # 40 tasks: 10 easy / 20 medium / 10 hard -> 2 / 4 / 2 of 8
    assert sorted(tiers[t] for t in picked).count("medium") == 4
    assert sorted(tiers[t] for t in picked).count("easy") == 2

    s3_again, _ = s3_stub()
    stub_aws({"s3": s3_again})
    again = harbor.smoldataenvs_rows("ds-1", "us-east-1", "bkt", print, train_n=8, seed=7)
    assert [r["payload"]["task_id"] for r in again["train"]] == picked


def test_rows_point_at_task_and_shared_input_uris(hub, stub_aws):
    hub(
        FakeHub(
            {
                harbor.SMOL_TRAIN: manifest_rows(6, ["easy"]),
                harbor.SMOL_TEST: manifest_rows(2, ["easy"]),
            },
            sizes={f"owner__ds{i}": 10 for i in range(3)},
        )
    )
    s3, stored = s3_stub()
    stub_aws({"s3": s3})
    out = harbor.smoldataenvs_rows("ds-1", "us-east-1", "bkt", print, all_tasks=True)
    row = next(r for r in out["train"] if r["payload"]["task_id"] == "0004_task")
    base = "s3://bkt/datasets/ds-1/smoldataenvs"
    assert row["payload"] == {
        "task_id": "0004_task",
        "task_uri": f"{base}/tasks/0004_task/",
        "input_uri": f"{base}/inputs/owner__ds1/",
    }
    assert row["prompt"] == [{"role": "user", "content": "Question for 0004_task"}]
    assert "datasets/ds-1/smoldataenvs/tasks/0004_task/tests/grader.py" in stored
    assert "datasets/ds-1/smoldataenvs/tasks/0004_task/environment/Dockerfile" not in stored
    # the held-out test split is used for in-training validation as well
    assert out["val"] == out["test"] and len(out["test"]) == 2


def test_each_input_prefix_is_uploaded_once_and_reused_on_rerun(hub, stub_aws):
    fake = hub(
        FakeHub(
            {
                harbor.SMOL_TRAIN: manifest_rows(9, ["easy"]),
                harbor.SMOL_TEST: manifest_rows(3, ["easy"]),
            },
            sizes={f"owner__ds{i}": 9 for i in range(3)},
        )
    )
    s3, stored = s3_stub()
    stub_aws({"s3": s3})
    harbor.smoldataenvs_rows("ds-1", "us-east-1", "bkt", print, all_tasks=True)
    assert sorted(fake.downloaded_prefixes) == ["owner__ds0", "owner__ds1", "owner__ds2"]
    uploads = len([c for c in s3.calls if c[0] == "upload_file"])

    fake.downloaded_prefixes.clear()
    s3_rerun, _ = s3_stub(stored)
    stub_aws({"s3": s3_rerun})
    harbor.smoldataenvs_rows("ds-1", "us-east-1", "bkt", print, all_tasks=True)
    assert fake.downloaded_prefixes == []
    assert not [c for c in s3_rerun.calls if c[0] == "upload_file"]
    assert uploads > 0


def test_tasks_whose_inputs_exceed_the_cap_are_skipped(hub, stub_aws):
    hub(
        FakeHub(
            {
                harbor.SMOL_TRAIN: manifest_rows(6, ["easy"]),
                harbor.SMOL_TEST: manifest_rows(3, ["easy"]),
            },
            sizes={"owner__ds0": 5_000, "owner__ds1": 10, "owner__ds2": 10},
        )
    )
    s3, _ = s3_stub()
    stub_aws({"s3": s3})
    logs: list[str] = []
    out = harbor.smoldataenvs_rows(
        "ds-1", "us-east-1", "bkt", logs.append, all_tasks=True, max_input_bytes=1_000
    )
    assert {r["payload"]["task_id"] for r in out["train"]} == {
        "0001_task",
        "0002_task",
        "0004_task",
        "0005_task",
    }
    assert len(out["test"]) == 2
    assert out["skipped"] == 3
    assert any("skipped 3" in m for m in logs)


def test_estimate_reports_tasks_and_bytes_without_uploading(hub):
    hub(
        FakeHub(
            {
                harbor.SMOL_TRAIN: manifest_rows(6, ["easy"]),
                harbor.SMOL_TEST: manifest_rows(3, ["easy"]),
            },
            sizes={"owner__ds0": 5_000, "owner__ds1": 10, "owner__ds2": 20},
        )
    )
    est = harbor.smoldataenvs_estimate(all_tasks=True, max_input_bytes=1_000)
    assert est == {"train": 4, "test": 2, "skipped": 3, "input_bytes": 30}


def test_tasks_with_different_environment_images_are_refused(hub, stub_aws):
    hub(
        FakeHub(
            {
                harbor.SMOL_TRAIN: manifest_rows(3, ["easy"]),
                harbor.SMOL_TEST: manifest_rows(1, ["easy"]),
            },
            sizes={f"owner__ds{i}": 10 for i in range(3)},
            images={"0002_task": "docker.io/other/image:1"},
        )
    )
    s3, _ = s3_stub()
    stub_aws({"s3": s3})
    with pytest.raises(AppError) as e:
        harbor.smoldataenvs_rows("ds-1", "us-east-1", "bkt", print, all_tasks=True)
    assert e.value.code == "dataset.harbor_mixed_images"
    assert not [c for c in s3.calls if c[0] == "upload_file"]


def _wait(jid: str):
    import time

    from app.core.db import session_scope
    from app.jobs import engine as eng
    from app.models import Job

    for _ in range(200):
        with session_scope() as s:
            j = s.get(Job, jid)
            if j.status in eng.TERMINAL:
                return j.status, j.error_code
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_console_imports_smoldataenvs_with_its_options(client, hub, stub_aws):
    from app.services import project as proj

    proj.save_region("us-east-1", {"status": "ready", "bucket": "bkt"})
    hub(
        FakeHub(
            {
                harbor.SMOL_TRAIN: manifest_rows(12, ["easy", "hard"]),
                harbor.SMOL_TEST: manifest_rows(3, ["easy"]),
            },
            sizes={"owner__ds0": 3 * 1024**3, "owner__ds1": 10, "owner__ds2": 10},
        )
    )
    s3, _ = s3_stub()
    stub_aws({"s3": s3})
    body = {"template_id": "multi_harness", "region": "us-east-1", "train_tasks": 6}
    est = client.post("/api/datasets/builtin/estimate", json={**body, "max_input_gb": 2})
    assert est.json() == {"train": 4, "test": 2, "skipped": 3, "input_bytes": 20}

    r = client.post("/api/datasets/builtin", json={**body, "max_input_gb": 2})
    assert _wait(r.json()["job_id"]) == ("succeeded", None)
    d = client.get(f"/api/datasets/{r.json()['id']}").json()
    assert d["status"] == "ready"
    assert d["stats"] == {"train": 4, "val": 2, "test": 2, "skipped": 3}
