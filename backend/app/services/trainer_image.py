"""Trainer images (x86_64 CUDA 13 + verl + toolkit), built by CodeBuild.

The platform host is arm64 and the image is ~25 GB of CUDA wheels, so the build
runs in a privileged x86 CodeBuild project. Source = S3 zip of
trainer_image/{Dockerfile,buildspec.yml} + the toolkit's git-tracked files.
Images are keyed by (region, profile, toolkit revision).
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from ..core import aws
from ..core.config import REPO_ROOT, get_settings
from ..core.errors import AppError

PROJECT = "tuningpad-trainer-build"
LOG_GROUP = "/aws/codebuild/tuningpad-trainer"
BUILD_DIR = REPO_ROOT / "trainer_image"
PROFILES = ("fsdp", "megatron")


def toolkit_revision() -> str:
    """Short commit + a content hash of uncommitted changes (so edits rebuild)."""
    tk = get_settings().toolkit_path

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tk, capture_output=True, text=True, timeout=60
        ).stdout.strip()

    head = git("rev-parse", "--short=10", "HEAD")
    if not head:
        raise AppError("trainer.toolkit_not_git", f"{tk} is not a git checkout")
    diff = git("status", "--porcelain") + git("diff", "HEAD")
    if diff:
        return f"{head}-d{hashlib.sha256(diff.encode()).hexdigest()[:8]}"
    return head


def image_tag(profile: str, revision: str) -> str:
    if profile not in PROFILES:
        raise AppError("trainer.bad_profile", f"profile must be one of {PROFILES}")
    return f"{profile}-{revision}"


def package_source(dest: Path) -> Path:
    tk = get_settings().toolkit_path
    files = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=tk,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.split("\n")
    skip = (".venv/", "docs/site/", "dist/")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("Dockerfile", "buildspec.yml"):
            zf.write(BUILD_DIR / name, name)
        for f in files:
            if not f or f.startswith(skip) or not (tk / f).is_file():
                continue
            zf.write(tk / f, f"toolkit/{f}")
    return dest


def upload_source(region: str, bucket: str, revision: str) -> str:
    key = f"build/trainer-src-{revision}.zip"
    with tempfile.TemporaryDirectory() as d:
        z = package_source(Path(d) / "src.zip")
        aws.client("s3", region).upload_file(str(z), bucket, key)
    return f"{bucket}/{key}"


def ensure_project(region: str, role_arn: str, source_location: str) -> None:
    cb = aws.client("codebuild", region)
    spec = {
        "source": {"type": "S3", "location": source_location, "buildspec": "buildspec.yml"},
        "artifacts": {"type": "NO_ARTIFACTS"},
        "environment": {
            "type": "LINUX_CONTAINER",
            "image": "aws/codebuild/standard:7.0",
            "computeType": "BUILD_GENERAL1_LARGE",  # 8 vCPU / 128 GB disk, ~$1.2/h
            "privilegedMode": True,
        },
        "serviceRole": role_arn,
        "timeoutInMinutes": 240,
        "logsConfig": {"cloudWatchLogs": {"status": "ENABLED", "groupName": LOG_GROUP}},
    }
    existing = cb.batch_get_projects(names=[PROJECT]).get("projects", [])
    if existing:
        cb.update_project(name=PROJECT, **spec)
    else:
        cb.create_project(
            name=PROJECT, tags=[{"key": t["Key"], "value": t["Value"]} for t in aws.tags()], **spec
        )


def start_build(region: str, source_location: str, profile: str, image_uri: str) -> str:
    r = aws.client("codebuild", region).start_build(
        projectName=PROJECT,
        sourceLocationOverride=source_location,
        environmentVariablesOverride=[
            {"name": "PROFILE", "value": profile, "type": "PLAINTEXT"},
            {"name": "IMAGE_URI", "value": image_uri, "type": "PLAINTEXT"},
        ],
    )
    return r["build"]["id"]


def build_status(region: str, build_id: str) -> dict[str, Any]:
    b = aws.client("codebuild", region).batch_get_builds(ids=[build_id])["builds"][0]
    return {
        "status": b["buildStatus"],
        "phase": b.get("currentPhase"),
        "logs": b.get("logs", {}),
        "phases": [
            {"name": p["phaseType"], "status": p.get("phaseStatus")} for p in b.get("phases", [])
        ],
    }


def build_log_tail(region: str, build_id: str, token: str | None) -> tuple[list[str], str | None]:
    stream = build_id.split(":")[-1]
    kwargs: dict[str, Any] = {
        "logGroupName": LOG_GROUP,
        "logStreamName": stream,
        "startFromHead": True,
    }
    if token:
        kwargs["nextToken"] = token
    try:
        r = aws.client("logs", region).get_log_events(**kwargs)
    except ClientError:
        return [], token
    return [e["message"].rstrip() for e in r.get("events", [])], r.get("nextForwardToken")


def image_exists(region: str, repo: str, tag: str) -> bool:
    try:
        aws.client("ecr", region).describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in {"ImageNotFoundException", "RepositoryNotFoundException"}:
            return False
        raise
