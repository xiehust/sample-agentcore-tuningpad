"""Agent pipelines: agent.build, agent.import, agent.deploy (runtime + smoke)."""

from __future__ import annotations

from typing import Any

from ..core.db import session_scope
from ..core.errors import AppError, NotFound
from ..jobs.engine import Stage, StageContext, register
from ..models import Agent, AgentRuntime, Dataset
from ..services import agents as svc
from ..services import project as proj
from ..templates_lib import get_template


def load_agent(agent_id: str) -> dict[str, Any]:
    with session_scope() as s:
        a = s.get(Agent, agent_id)
        if not a:
            raise NotFound("agent.not_found", f"agent {agent_id} not found")
        return {
            "id": a.id,
            "name": a.name,
            "source": a.source,
            "template_id": a.template_id,
            "config": dict(a.config or {}),
            "image_uri": a.image_uri,
            "status": a.status,
            "checks": dict(a.checks or {}),
        }


def save_agent(agent_id: str, **fields: Any) -> None:
    with session_scope() as s:
        a = s.get(Agent, agent_id)
        if a:
            for k, v in fields.items():
                if k in {"config", "checks"} and isinstance(v, dict):
                    v = {**(getattr(a, k) or {}), **v}
                setattr(a, k, v)


def load_runtime(rt_id: str) -> dict[str, Any]:
    with session_scope() as s:
        r = s.get(AgentRuntime, rt_id)
        if not r:
            raise NotFound("runtime.not_found", f"runtime {rt_id} not found")
        return {
            "id": r.id,
            "agent_id": r.agent_id,
            "cluster_id": r.cluster_id,
            "region": r.region,
            "network_mode": r.network_mode,
            "runtime_id": r.runtime_id,
            "runtime_arn": r.runtime_arn,
            "image_uri": r.image_uri,
            "platform_version": r.platform_version,
            "status": r.status,
        }


def save_runtime(rt_id: str, **fields: Any) -> None:
    with session_scope() as s:
        r = s.get(AgentRuntime, rt_id)
        if r:
            for k, v in fields.items():
                setattr(r, k, v)


# ---------------- build ----------------


def stage_context(ctx: StageContext) -> None:
    a = load_agent(ctx.target_id)
    save_agent(a["id"], status="building")
    path = svc.prepare_context(a)
    ctx.set("context_dir", str(path))
    ctx.log(f"context at {path}: {sorted(p.name for p in path.iterdir())}")


def stage_checks(ctx: StageContext) -> None:
    from pathlib import Path

    result = svc.static_checks(Path(ctx.context["context_dir"]))
    save_agent(ctx.target_id, checks={"static": result})
    for w in result["warnings"]:
        ctx.log(f"warning: {w}")
    if result["errors"]:
        raise AppError("agent.contract", "; ".join(result["errors"]), detail=result)


def stage_build(ctx: StageContext) -> None:
    from pathlib import Path

    tag = f"tuningpad/{ctx.target_id}:latest"
    svc.docker_build(Path(ctx.context["context_dir"]), tag, ctx.log)
    ctx.set("local_tag", tag)


def stage_probe(ctx: StageContext) -> None:
    result = svc.local_probe(ctx.context["local_tag"], ctx.log)
    save_agent(ctx.target_id, checks={"probe": result})


def stage_push(ctx: StageContext) -> None:
    region = ctx.payload["region"]
    res = proj.require_region(region)
    import time

    remote = f"{res['agent_repo_uri']}:{ctx.target_id}-{int(time.time())}"
    svc.docker_push(ctx.context["local_tag"], remote, region, ctx.log)
    info = svc.inspect_image(remote)
    save_agent(ctx.target_id, image_uri=remote, checks={"image": info})
    ctx.log(f"pushed {remote} ({info['size_bytes'] / 1024**2:.0f} MB)")


def _built(target: str, status: str, error: str | None) -> None:
    save_agent(target, status="ready" if status == "succeeded" else "failed")


register(
    "agent.build",
    [
        Stage("context", stage_context),
        Stage("checks", stage_checks),
        Stage("build", stage_build),
        Stage("probe", stage_probe),
        Stage("push", stage_push),
    ],
    on_finish=_built,
)


def stage_import(ctx: StageContext) -> None:
    a = load_agent(ctx.target_id)
    info = svc.inspect_image(a["image_uri"])
    save_agent(a["id"], checks={"image": info})
    ctx.log(f"image ok: {info}")


register("agent.import", [Stage("inspect", stage_import)], on_finish=_built)


# ---------------- deploy + smoke ----------------


def smoke_payloads(
    agent: dict[str, Any], override: list[dict] | None, region: str | None = None
) -> list[dict]:
    if override:
        return override
    if agent["config"].get("smoke_payloads"):
        return agent["config"]["smoke_payloads"]
    if agent["source"] == "template":
        t = get_template(agent["template_id"])
        if t.get("smoke_payloads"):
            return t["smoke_payloads"]
        # data-driven templates (OfficeBench): take two rows of the generated dataset — in
        # the runtime's Region: its role reads only that Region's bucket (the payload is S3
        # URIs), so a newer dataset elsewhere fails the smoke with AccessDenied
        with session_scope() as s:
            q = s.query(Dataset).filter(
                Dataset.template_id == agent["template_id"], Dataset.status == "ready"
            )
            if region:
                q = q.filter(Dataset.region == region)
            ds = q.order_by(Dataset.created_at.desc()).first()
            if ds and ds.sample:
                return [row["payload"] for row in ds.sample[:2]]
    return []


def runtime_network(rt: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """(networkConfiguration, runtime-name suffix) for a runtime row."""
    if rt["network_mode"] != "VPC":
        return svc.network_config("PUBLIC"), "smoke"
    from ..pipelines.cluster import load as load_cluster

    c = load_cluster(rt["cluster_id"])
    net = c["network"]
    if not net.get("sg_acr"):
        raise AppError(
            "agent.cluster_not_ready",
            "cluster security groups are not installed yet (repair the cluster)",
        )
    return svc.network_config("VPC", net.get("private_subnets"), [net["sg_acr"]]), c["name"]


def deploy_and_wait(
    ctx: StageContext,
    *,
    region: str,
    name: str,
    image_uri: str,
    role_arn: str,
    network: dict[str, Any],
    vpc: bool,
    platform: str,
    existing_id: str | None,
    on_ids,
    on_ready,
    environment: dict[str, str] | None = None,
) -> None:
    """Create/update one AgentCore runtime and wait for READY: waits out an in-progress
    runtime first (ConflictException otherwise) and, in VPC mode, retries once with only the
    subnets in AgentCore-supported AZs. `on_ids(out)` persists the runtime id/arn as soon as
    they exist, `on_ready(info)` the version AgentCore applied."""
    ready_timeout = svc.READY_TIMEOUT_S[platform]
    existing_id = existing_id or (svc.find_runtime(region, name) or {}).get("agentRuntimeId")
    if existing_id:
        # an update while CREATING/UPDATING returns ConflictException (V2 stays there minutes)
        ctx.wait_until(
            lambda: not svc.runtime_busy(svc.runtime_status(region, existing_id)[0]),
            timeout_s=ready_timeout,
            interval_s=10,
            what="runtime to leave its in-progress state",
        )
    ctx.log(f"platform version {platform}")
    env = {"environment": environment} if environment else {}
    out = svc.deploy_runtime(
        region, name, image_uri, role_arn, network, existing_id, platform, **env
    )
    on_ids(out)
    save_ids = dict(out)
    ctx.log(f"runtime {name}: {out}")

    def ready():
        info = svc.runtime_info(region, save_ids["runtime_id"])
        status, reason = info["status"], info["reason"]
        ctx.detail(status)
        if "FAILED" in status:
            raise AppError(
                "agent.runtime_failed", f"runtime {status}: {reason}", detail={"reason": reason}
            )
        if status == "READY":
            on_ready(info)
            return True
        return False

    try:
        ctx.wait_until(ready, timeout_s=ready_timeout, interval_s=10, what="runtime READY")
    except AppError as e:
        supported = svc.supported_az_ids((e.detail or {}).get("reason") or "")
        if not vpc or not supported:
            raise
        # AgentCore VPC mode is offered in a subset of AZs: keep only those subnets
        subnets = svc.subnets_in_azs(region, network["networkModeConfig"]["subnets"], supported)
        if not subnets:
            raise AppError(
                "agent.no_supported_subnet",
                f"no cluster subnet in an AgentCore-supported AZ {supported}",
            ) from e
        ctx.log(f"retrying with subnets in supported AZs {supported}: {subnets}")
        svc.delete_runtime(region, out["runtime_id"])
        ctx.wait_until(
            lambda: svc.find_runtime(region, name) is None,
            timeout_s=300,
            interval_s=10,
            what="failed runtime deletion",
        )
        network = svc.network_config("VPC", subnets, network["networkModeConfig"]["securityGroups"])
        out = svc.deploy_runtime(region, name, image_uri, role_arn, network, None, platform, **env)
        on_ids(out)
        save_ids.update(out)
        ctx.wait_until(ready, timeout_s=ready_timeout, interval_s=10, what="runtime READY")


def stage_deploy(ctx: StageContext) -> None:
    rt = load_runtime(ctx.target_id)
    agent = load_agent(rt["agent_id"])
    if not agent["image_uri"] or agent["status"] != "ready":
        raise AppError("agent.not_built", "build or import the agent image first")
    res = proj.require_region(rt["region"])
    network, suffix = runtime_network(rt)
    platform = svc.resolve_platform_version(
        rt["region"], ctx.payload.get("platform_version") or rt["platform_version"]
    )
    first = {"done": False}

    def on_ids(out: dict[str, str]) -> None:
        if not first["done"]:  # the first save also marks the row as deploying
            first["done"] = True
            save_runtime(
                rt["id"],
                status="deploying",
                image_uri=agent["image_uri"],
                platform_version=platform,
                **out,
            )
        else:
            save_runtime(rt["id"], **out)

    deploy_and_wait(
        ctx,
        region=rt["region"],
        name=svc.runtime_name(agent["name"], suffix),
        image_uri=agent["image_uri"],
        role_arn=res["acr_role_arn"],
        network=network,
        vpc=rt["network_mode"] == "VPC",
        platform=platform,
        existing_id=rt["runtime_id"],
        on_ids=on_ids,
        on_ready=lambda info: save_runtime(rt["id"], platform_version=info["platform_version"]),
    )


def save_obs(rt_id: str, **fields: Any) -> None:
    with session_scope() as s:
        r = s.get(AgentRuntime, rt_id)
        if r:
            r.obs = {**(r.obs or {}), **fields}


def ensure_obs_runtime(ctx: StageContext, rt_id: str) -> str:
    """The eval-only OTEL twin of a training runtime: same image, role and network, plus
    `obs_environment`. Reused while READY on the training runtime's image, updated when the
    image moved, created on first use. Returns its ARN. No contract smoke: the image is the
    one the training runtime already passed with."""
    rt = load_runtime(rt_id)
    if rt["status"] != "ready" or not rt["image_uri"] or rt["network_mode"] != "VPC":
        raise AppError("eval.runtime", "the agent runtime must be ready and deployed to a cluster")
    with session_scope() as s:
        obs = dict(s.get(AgentRuntime, rt_id).obs or {})
    if (
        obs.get("status") == "ready"
        and obs.get("image_uri") == rt["image_uri"]
        and obs.get("runtime_arn")
    ):
        ctx.log(f"eval trace runtime {obs['runtime_id']} is up to date")
        return obs["runtime_arn"]
    agent = load_agent(rt["agent_id"])
    res = proj.require_region(rt["region"])
    network, suffix = runtime_network(rt)
    name = svc.obs_runtime_name(svc.runtime_name(agent["name"], suffix))
    platform = rt["platform_version"] or "V1"  # same platform as the training twin
    save_obs(rt_id, status="deploying", image_uri=rt["image_uri"], error=None)
    try:
        deploy_and_wait(
            ctx,
            region=rt["region"],
            name=name,
            image_uri=rt["image_uri"],
            role_arn=res["acr_role_arn"],
            network=network,
            vpc=True,
            platform=platform,
            existing_id=obs.get("runtime_id"),
            on_ids=lambda out: save_obs(rt_id, **out),
            on_ready=lambda info: save_obs(rt_id, platform_version=info["platform_version"]),
            environment=svc.obs_environment(name),
        )
    except Exception as e:
        save_obs(rt_id, status="failed", error=str(e)[:500])
        raise
    save_obs(rt_id, status="ready")
    with session_scope() as s:
        return s.get(AgentRuntime, rt_id).obs["runtime_arn"]


def stage_smoke(ctx: StageContext) -> None:
    rt = load_runtime(ctx.target_id)
    agent = load_agent(rt["agent_id"])
    payloads = smoke_payloads(agent, ctx.payload.get("smoke_payloads"), rt["region"])
    if ctx.payload.get("skip_smoke"):
        ctx.log("smoke skipped by request")
        return
    res = proj.require_region(rt["region"])
    result = svc.smoke(
        rt["region"], rt["runtime_arn"], res["bucket"], agent["id"], payloads, ctx.log
    )
    with session_scope() as s:
        r = s.get(AgentRuntime, rt["id"])
        if r:
            r.last_smoke = result
    if not result["ok"]:
        raise AppError(
            "agent.smoke_failed",
            f"{result['passed']}/{result['total']} smoke rollouts returned numeric rewards",
            detail=result,
        )


def _deployed(target: str, status: str, error: str | None) -> None:
    save_runtime(target, status="ready" if status == "succeeded" else "failed")


register(
    "agent.deploy",
    [Stage("deploy", stage_deploy), Stage("smoke", stage_smoke)],
    on_finish=_deployed,
)
