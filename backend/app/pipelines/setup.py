"""`setup.region`: per-region project resources (bucket, roles, ECR repositories)."""

from __future__ import annotations

from ..core import aws
from ..jobs.engine import Stage, StageContext, register
from ..services import project as proj


def _region(ctx: StageContext) -> str:
    return ctx.payload["region"]


def stage_identity(ctx: StageContext) -> None:
    ident = aws.client("sts", _region(ctx)).get_caller_identity()
    proj.save_identity(ident["Account"], ident["Arn"])
    ctx.set("account", ident["Account"])
    ctx.set("names", proj.names(ident["Account"], _region(ctx)))
    proj.save_region(_region(ctx), {"status": "provisioning"})
    ctx.log(f"account {ident['Account']} as {ident['Arn']}")


def stage_bucket(ctx: StageContext) -> None:
    bucket = ctx.context["names"]["bucket"]
    proj.ensure_bucket(_region(ctx), bucket)
    proj.save_region(_region(ctx), {"bucket": bucket})
    ctx.log(f"bucket s3://{bucket} ready (public access blocked, SSE, lifecycle)")


def stage_roles(ctx: StageContext) -> None:
    region, account = _region(ctx), ctx.context["account"]
    n = ctx.context["names"]
    trust, policy = proj.acr_role_documents(account, region, n["bucket"])
    acr_arn = proj.ensure_role(region, n["acr_role"], trust, {"TuningPadAgentRuntime": policy})
    trust, policy = proj.codebuild_role_documents(account, region, n["bucket"])
    cb_arn = proj.ensure_role(region, n["codebuild_role"], trust, {"TuningPadCodeBuild": policy})
    proj.save_region(region, {"acr_role_arn": acr_arn, "codebuild_role_arn": cb_arn})
    ctx.log(f"roles ready: {acr_arn}, {cb_arn}")
    ctx.sleep(8)  # IAM propagation before first AssumeRole by the services


def stage_ecr(ctx: StageContext) -> None:
    region, n = _region(ctx), ctx.context["names"]
    agent_uri = proj.ensure_ecr_repo(region, n["agent_repo"])
    trainer_uri = proj.ensure_ecr_repo(region, n["trainer_repo"])
    proj.save_region(region, {"agent_repo_uri": agent_uri, "trainer_repo_uri": trainer_uri})
    ctx.log(f"ECR {agent_uri}, {trainer_uri}")


def _finish(target: str, status: str, error: str | None) -> None:
    proj.save_region(
        target, {"status": "ready" if status == "succeeded" else "failed", "error": error}
    )


register(
    "setup.region",
    [
        Stage("identity", stage_identity),
        Stage("bucket", stage_bucket),
        Stage("roles", stage_roles),
        Stage("ecr", stage_ecr),
    ],
    on_finish=_finish,
)
