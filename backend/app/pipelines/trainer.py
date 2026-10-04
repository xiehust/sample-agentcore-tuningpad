"""`trainer.build`: CodeBuild a trainer image for (region, profile, toolkit revision)."""

from __future__ import annotations

from typing import Any

from ..core.db import new_id, session_scope
from ..core.errors import AppError
from ..jobs.engine import Stage, StageContext, get_engine, register
from ..models import TrainerImage
from ..services import project as proj
from ..services import trainer_image as ti


def _load(tid: str) -> dict[str, Any]:
    with session_scope() as s:
        t = s.get(TrainerImage, tid)
        return {
            "id": t.id,
            "profile": t.profile,
            "region": t.region,
            "toolkit_sha": t.toolkit_sha,
            "image_uri": t.image_uri,
            "status": t.status,
            "build_id": t.build_id,
        }


def _save(tid: str, **fields: Any) -> None:
    with session_scope() as s:
        t = s.get(TrainerImage, tid)
        if t:
            for k, v in fields.items():
                setattr(t, k, v)


def find_or_create(region: str, profile: str) -> tuple[str, bool]:
    """(trainer image id, ready?) for the current toolkit revision; starts a build if absent."""
    rev = ti.toolkit_revision()
    with session_scope() as s:
        t = (
            s.query(TrainerImage)
            .filter(
                TrainerImage.region == region,
                TrainerImage.profile == profile,
                TrainerImage.toolkit_sha == rev,
            )
            .order_by(TrainerImage.created_at.desc())
            .first()
        )
        if t and t.status in ("ready", "building", "queued"):
            return t.id, t.status == "ready"
    res = proj.require_region(region)
    tag = ti.image_tag(profile, rev)
    uri = f"{res['trainer_repo_uri']}:{tag}"
    tid = new_id("ti")
    ready = ti.image_exists(region, proj.TRAINER_ECR_REPO, tag)
    with session_scope() as s:
        s.add(
            TrainerImage(
                id=tid,
                profile=profile,
                region=region,
                toolkit_sha=rev,
                image_uri=uri,
                status="ready" if ready else "queued",
            )
        )
    if not ready:
        get_engine().start("trainer.build", tid, {})
    return tid, ready


def stage_source(ctx: StageContext) -> None:
    t = _load(ctx.target_id)
    res = proj.require_region(t["region"])
    _save(t["id"], status="building")
    loc = ti.upload_source(t["region"], res["bucket"], t["toolkit_sha"])
    ti.ensure_project(t["region"], res["codebuild_role_arn"], loc)
    ctx.set("source", loc)
    ctx.log(f"source s3://{loc}")


def stage_build(ctx: StageContext) -> None:
    t = _load(ctx.target_id)
    build_id = ctx.context.get("build_id")
    if not build_id:
        build_id = ti.start_build(t["region"], ctx.context["source"], t["profile"], t["image_uri"])
        ctx.set("build_id", build_id)
        _save(t["id"], build_id=build_id)
        ctx.log(f"CodeBuild {build_id}")
    token: list[str | None] = [ctx.context.get("log_token")]

    def probe():
        lines, nxt = ti.build_log_tail(t["region"], build_id, token[0])
        for line in lines:
            ctx.log(line)
        token[0] = nxt
        st = ti.build_status(t["region"], build_id)
        ctx.detail(f"{st['phase'] or ''} {st['status']}")
        if st["status"] in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT"):
            raise AppError("trainer.build_failed", f"CodeBuild {st['status']} in {st['phase']}")
        return st["status"] == "SUCCEEDED"

    ctx.wait_until(probe, timeout_s=5 * 3600, interval_s=30, what="trainer image build")


def _done(target: str, status: str, error: str | None) -> None:
    _save(target, status="ready" if status == "succeeded" else "failed")


register(
    "trainer.build", [Stage("source", stage_source), Stage("build", stage_build)], on_finish=_done
)
