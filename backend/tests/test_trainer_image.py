"""Trainer source identity and packaging, with local Git and cloud stubs only."""

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.core.db import session_scope
from app.core.errors import AppError
from app.jobs import engine
from app.models import TrainerImage
from app.pipelines import trainer
from app.services import project, toolkit_source, trainer_image
from tests.conftest import StubClient


def test_trainer_zip_contains_only_tracked_inputs(toolkit_repo, tmp_path):
    f = toolkit_repo
    dest = trainer_image.package_source(tmp_path / "source.zip", expected_revision=f.revision)
    with zipfile.ZipFile(dest) as z:
        assert set(z.namelist()) == {
            "Dockerfile",
            "buildspec.yml",
            "toolkit/README-link.md",
            *(f"toolkit/{name}" for name in f.files),
        }
        for name, content in f.files.items():
            assert z.read(f"toolkit/{name}").decode() == content
        assert z.read("toolkit/README-link.md") == z.read("toolkit/README.md")
        for name in ("Dockerfile", "buildspec.yml"):
            assert z.read(name) == (trainer_image.BUILD_DIR / name).read_bytes()
    assert not list(f.root.rglob("__pycache__"))


@pytest.mark.parametrize("bad_revision", ["short", "dirty", "other"])
def test_expected_revision_rejected_before_packaging_or_upload(
    bad_revision, toolkit_repo, tmp_path, stub_aws
):
    f = toolkit_repo
    revision = {
        "short": f.revision[:10],
        "dirty": f"{f.revision[:10]}-d12345678",
        "other": "0" * 40,
    }[bad_revision]
    dest = tmp_path / "source.zip"
    with pytest.raises(AppError) as caught:
        trainer_image.package_source(dest, expected_revision=revision)
    assert caught.value.code == "toolkit.source_invalid"
    assert caught.value.detail == {"reason": "expected_revision_mismatch"}
    assert not dest.exists()
    s3 = StubClient(upload_file=None)
    stub_aws({"s3": s3})
    with pytest.raises(AppError, match="pinned Git source"):
        trainer_image.upload_source("us-east-1", "b", revision)
    assert not s3.calls


@pytest.mark.parametrize("corruption", ["untracked", "dirty", "metadata"])
def test_invalid_source_never_uploads(corruption, toolkit_repo, toolkit_installation, stub_aws):
    f = toolkit_repo
    if corruption == "untracked":
        (f.root / "extra.py").write_text("untracked input")
    elif corruption == "dirty":
        (f.root / "README.md").write_text("dirty input")
    else:
        toolkit_installation["vcs_info"]["commit_id"] = "0" * 40
    s3 = StubClient(upload_file=None)
    stub_aws({"s3": s3})
    with pytest.raises(AppError) as caught:
        trainer_image.upload_source("us-east-1", "b", f.revision)
    assert caught.value.code == "toolkit.source_invalid"
    assert not s3.calls


def test_upload_keeps_key_format_and_verified_zip(toolkit_repo, stub_aws):
    f = toolkit_repo
    uploaded = []

    def upload(_args):
        path, bucket, key = _args
        with zipfile.ZipFile(path) as z:
            assert z.read("toolkit/uv.lock").decode() == f.files["uv.lock"]
            assert "toolkit/ignored-secret" not in z.namelist()
        uploaded.append((path, bucket, key))

    stub_aws({"s3": StubClient(upload_file=upload)})
    key = f"build/trainer-src-{f.revision}.zip"
    assert trainer_image.upload_source("us-east-1", "bucket", f.revision) == f"bucket/{key}"
    assert uploaded[0][1:] == ("bucket", key)
    assert not Path(uploaded[0][0]).exists()


def test_revision_reads_only_authoritative_full_sha(toolkit_installation, monkeypatch):
    revision = toolkit_source.source_spec()[1]

    def forbidden(*args, **kwargs):
        raise AssertionError("image selection must not inspect or acquire a checkout")

    for name in ("resolve_source", "inspect_source", "_git", "validate_installation"):
        monkeypatch.setattr(toolkit_source, name, forbidden)
    assert trainer_image.toolkit_revision() == revision
    assert len(revision) == 40
    assert trainer_image.image_tag("fsdp", revision) == f"fsdp-{revision}"


@pytest.mark.parametrize("ecr_ready", [False, True])
def test_full_revision_never_reuses_legacy_short_or_dirty_records(
    toolkit_installation, monkeypatch, stub_aws, ecr_ready
):
    revision = toolkit_source.source_spec()[1]
    region, profile = "us-east-1", "fsdp"
    with session_scope() as s:
        for i, old in enumerate((revision[:10], f"{revision[:10]}-d12345678")):
            s.add(
                TrainerImage(
                    id=f"ti-old-{i}",
                    profile=profile,
                    region=region,
                    toolkit_sha=old,
                    image_uri=f"repo:{profile}-{old}",
                    status="ready",
                )
            )
    project.save_region(region, {"status": "ready", "trainer_repo_uri": "repo"})

    def describe(**kwargs):
        if not ecr_ready:
            raise ClientError({"Error": {"Code": "ImageNotFoundException"}}, "DescribeImages")
        return {"imageDetails": []}

    ecr = StubClient(describe_images=describe)
    stub_aws({"ecr": ecr})
    starts = []
    monkeypatch.setattr(engine.get_engine(), "start", lambda *args: starts.append(args))
    tid, ready = trainer.find_or_create(region, profile)
    assert ready is ecr_ready and not tid.startswith("ti-old-")
    with session_scope() as s:
        image = s.get(TrainerImage, tid)
        assert image.toolkit_sha == revision
        assert image.image_uri == f"repo:{profile}-{revision}"
        assert image.status == ("ready" if ecr_ready else "queued")
        assert s.query(TrainerImage).count() == 3
    assert ecr.calls == [
        (
            "describe_images",
            {
                "repositoryName": project.TRAINER_ECR_REPO,
                "imageIds": [{"imageTag": f"{profile}-{revision}"}],
            },
        )
    ]
    assert starts == ([] if ecr_ready else [("trainer.build", tid, {})])
    assert trainer.find_or_create(region, profile) == (tid, ecr_ready)
    assert len(ecr.calls) == 1


def test_source_stage_passes_recorded_sha_without_rewriting_it(toolkit_repo, monkeypatch):
    f = toolkit_repo
    with session_scope() as s:
        s.add(
            TrainerImage(
                id="ti-stage",
                profile="fsdp",
                region="us-east-1",
                toolkit_sha=f.revision,
                image_uri="repo:tag",
                status="queued",
            )
        )
    project.save_region(
        "us-east-1", {"status": "ready", "bucket": "b", "codebuild_role_arn": "role"}
    )
    calls, context = [], {}

    def upload(region, bucket, *, revision):
        calls.append((region, bucket, revision))
        return "b/source.zip"

    monkeypatch.setattr(trainer_image, "upload_source", upload)
    monkeypatch.setattr(trainer_image, "ensure_project", lambda *args: calls.append(args))
    ctx = SimpleNamespace(
        target_id="ti-stage", set=lambda k, v: context.update({k: v}), log=lambda m: None
    )
    trainer.stage_source(ctx)
    assert calls == [("us-east-1", "b", f.revision), ("us-east-1", "role", "b/source.zip")]
    assert context == {"source": "b/source.zip"}
    assert trainer._load("ti-stage")["toolkit_sha"] == f.revision
