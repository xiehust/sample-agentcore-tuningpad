"""export.merge · inference.deploy · inference.delete · eval.run"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core import aws
from ..core.db import session_scope
from ..core.errors import AppError
from ..jobs.engine import Stage, StageContext, register
from ..models import AgentRuntime, Dataset, Eval, Export, InferenceEndpoint, Run, TrainerImage
from ..services import datasets as dsvc
from ..services import kube
from ..services import project as proj
from ..services import serving as svc
from . import cluster as pc


def _save(model, oid: str, **fields: Any) -> None:
    with session_scope() as s:
        o = s.get(model, oid)
        if o:
            for k, v in fields.items():
                setattr(o, k, v)


def _get(model, oid: str) -> dict[str, Any]:
    with session_scope() as s:
        o = s.get(model, oid)
        if not o:
            raise AppError("not_found", f"{model.__tablename__} {oid} not found", status=404)
        return {c.name: getattr(o, c.name) for c in model.__table__.columns}


# ---------------- export ----------------


def stage_merge(ctx: StageContext) -> None:
    e = _get(Export, ctx.target_id)
    run = _get(Run, e["run_id"])
    c = pc.load(run["cluster_id"])
    bucket = proj.require_region(c["region"])["bucket"]
    with session_scope() as s:
        img = (
            s.query(TrainerImage)
            .filter(
                TrainerImage.region == c["region"],
                TrainerImage.status == "ready",
                TrainerImage.profile == run["spec"]["plan"]["profile"],
            )
            .order_by(TrainerImage.created_at.desc())
            .first()
        )
        if not img:
            raise AppError("export.no_image", "no ready trainer image for this run's profile")
        image = img.image_uri
    from ..catalog import models

    params_b = models.check_model(run["model_id"]).params_b
    group, mem = svc.merge_resources(params_b, run["compute"]["instance_group"])
    from ..services import pools

    provider = (
        pools.HYPERPOD if group == "system" else run["compute"].get("provider", pools.HYPERPOD)
    )
    job = svc.merge_job(
        export_id=e["id"],
        run_id=run["id"],
        step=e["step"],
        backend=run["spec"]["plan"]["profile"],
        image=image,
        bucket=bucket,
        group=group,
        region=c["region"],
        mem_gib=mem,
        node_selector=pools.node_selector(provider, group),
    )
    name = job["metadata"]["name"]
    if not kube.get(c["region"], c["eks_name"], "batch/v1", "Job", name):
        kube.apply(c["region"], c["eks_name"], job)
    _save(Export, e["id"], status="running", k8s_job=name, fsx_path=svc.export_dir(e["id"]))
    ctx.log(f"merge Job {name} on group {group} ({mem} GiB)")

    def done():
        j = kube.get(c["region"], c["eks_name"], "batch/v1", "Job", name) or {}
        st = j.get("status") or {}
        ctx.detail(
            f"active {st.get('active', 0)} · succeeded {st.get('succeeded', 0)}"
            f" · failed {st.get('failed', 0)}"
        )
        if (st.get("failed") or 0) > 1:
            raise AppError("export.failed", "merge job failed (see pod logs)")
        return (st.get("succeeded") or 0) >= 1

    ctx.wait_until(done, timeout_s=6 * 3600, interval_s=20, what="checkpoint merge")
    _save(Export, e["id"], status="succeeded", s3_uri=f"s3://{bucket}/exports/{e['id']}/")


def _export_done(target: str, status: str, error: str | None) -> None:
    if status != "succeeded":
        _save(Export, target, status="failed", error=error)


register("export.merge", [Stage("merge", stage_merge)], on_finish=_export_done)


# ---------------- inference ----------------


def stage_deploy_endpoint(ctx: StageContext) -> None:
    ep = _get(InferenceEndpoint, ctx.target_id)
    c = pc.load(ep["cluster_id"])
    region, eks = c["region"], c["eks_name"]
    if not c["network"].get("sg_nlb"):
        raise AppError("inference.cluster_not_ready", "cluster security groups missing (repair)")
    src = ep["model_source"]
    if src.get("kind") == "export":
        ex = _get(Export, src["export_id"])
        if ex["status"] != "succeeded":
            raise AppError("inference.export_not_ready", "the export has not finished")
        model = svc.export_dir(ex["id"])
    else:
        model = src["model_id"]
    from ..catalog import models

    base_model = src.get("base_model_id") or src.get("model_id")
    chk = models.check_model(base_model) if base_model else None
    if not kube.get(region, eks, "v1", "Secret", svc.endpoint_name(ep["id"])):
        kube.apply(region, eks, svc.secret_manifest(ep["id"], svc.api_key()))
    group = ep["instance_group"]
    from ..services import pools

    pool = pools.find(c, group)
    if not pool or group == "system":
        raise AppError("inference.no_group", f"instance group {group} not found")
    for m in svc.vllm_manifests(
        endpoint_id=ep["id"],
        model=model,
        served_name=ep["served_model_name"],
        group=group,
        instance_type=pool["instance_type"],
        tp=ep["tp"],
        replicas=ep["replicas"],
        tool_parser=chk.tool_call_parser if chk else None,
        reasoning_parser=chk.reasoning_parser if chk else None,
        sg_nlb=c["network"]["sg_nlb"],
        subnets=c["network"].get("private_subnets") or [],
        max_model_len=src.get("max_model_len"),
        node_selector=pools.node_selector(pool["provider"], group),
    ):
        kube.apply(region, eks, m)
    _save(InferenceEndpoint, ep["id"], status="deploying")

    def ready():
        st = svc.endpoint_state(region, eks, ep["id"])
        ctx.detail(f"{st['ready']}/{st['replicas']} ready · NLB {st['host'] or 'pending'}")
        return st if st["ready"] >= max(1, ep["replicas"]) and st["url"] else None

    st = ctx.wait_until(ready, timeout_s=3 * 3600, interval_s=20, what="vLLM ready + NLB")
    _save(InferenceEndpoint, ep["id"], url=st["url"], status="ready")
    ctx.log(f"serving {ep['served_model_name']} at {st['url']}")


def _endpoint_done(target: str, status: str, error: str | None) -> None:
    if status != "succeeded":
        _save(InferenceEndpoint, target, status="failed", error=error)


register("inference.deploy", [Stage("deploy", stage_deploy_endpoint)], on_finish=_endpoint_done)


def stage_delete_endpoint(ctx: StageContext) -> None:
    ep = _get(InferenceEndpoint, ctx.target_id)
    c = pc.load(ep["cluster_id"])
    name = svc.endpoint_name(ep["id"])
    for api, kind in (("v1", "Service"), ("apps/v1", "Deployment"), ("v1", "Secret")):
        kube.delete(c["region"], c["eks_name"], api, kind, name)
    _save(InferenceEndpoint, ep["id"], status="deleted", url=None)


register("inference.delete", [Stage("delete", stage_delete_endpoint)])


# ---------------- evaluation ----------------


def _payloads(dataset_id: str, split: str, limit: int) -> list[dict[str, Any]]:
    with session_scope() as s:
        d = s.get(Dataset, dataset_id)
        if not d or split not in (d.splits or {}):
            raise AppError("eval.bad_split", f"split {split} not found")
        region, key = d.region, d.splits[split]["s3_key"]
    local = dsvc.local_dir(dataset_id) / f"{split}.parquet"
    if not local.exists():
        bucket = proj.require_region(region)["bucket"]
        aws.client("s3", region).download_file(bucket, key, str(local))
    import pandas as pd

    df = pd.read_parquet(local).head(limit)
    return [dsvc._to_jsonable(p) for p in df["payload"]]


def stage_eval(ctx: StageContext) -> None:
    ev = _get(Eval, ctx.target_id)
    ep = _get(InferenceEndpoint, ev["endpoint_id"])
    if ep["status"] != "ready" or not ep["url"]:
        raise AppError("eval.endpoint_not_ready", "inference endpoint is not ready")
    with session_scope() as s:
        rt = s.get(AgentRuntime, ev["agent_runtime_id"])
        if not rt or rt.status != "ready" or rt.cluster_id != ep["cluster_id"]:
            raise AppError(
                "eval.runtime",
                "pick a ready agent runtime deployed to the endpoint's cluster (VPC)",
            )
        runtime_arn = rt.runtime_arn
    c = pc.load(ep["cluster_id"])
    bucket = proj.require_region(c["region"])["bucket"]
    key = svc.read_api_key(c["region"], c["eks_name"], ep["id"])
    payloads = _payloads(ev["dataset_id"], ev["split"], ev["limit"])
    _save(Eval, ev["id"], status="running")
    from ..services.agents import rollout_client

    client = rollout_client(
        agent_runtime_arn=runtime_arn,
        s3_bucket=bucket,
        exp_id=f"evals/{ev['id']}",
        base_url=ep["url"],
        model_id=ep["served_model_name"],
        api_key=key,
        tps_limit=8,
    )
    items: list[dict[str, Any]] = []
    out_path = Path(dsvc.local_dir(ev["id"])) / "results.jsonl"
    with out_path.open("w") as f:
        for it in client.run_batch(
            payloads, max_concurrent_sessions=32, timeout=float(ctx.payload.get("timeout_s", 1800))
        ):
            ctx.check_cancel()
            rec = {
                "index": it.index,
                "success": it.success,
                "result": {k: v for k, v in (it.result or {}).items() if k != "payload"},
                "error": it.error,
                "elapsed": getattr(it, "elapsed", None),
            }
            items.append(rec)
            f.write(json.dumps(rec, default=str) + "\n")
            if len(items) % 10 == 0:
                summ = svc.summarize_eval(items)
                ctx.detail(f"{len(items)}/{len(payloads)} · mean {summ['mean_reward']}")
                _save(Eval, ev["id"], summary=summ)
    summary = svc.summarize_eval(items)
    aws.client("s3", c["region"]).upload_file(
        str(out_path), bucket, f"evals/{ev['id']}/results.jsonl"
    )
    _save(
        Eval,
        ev["id"],
        summary=summary,
        status="succeeded",
        results_s3=f"s3://{bucket}/evals/{ev['id']}/results.jsonl",
    )
    ctx.log(f"eval summary {summary}")


def _eval_done(target: str, status: str, error: str | None) -> None:
    if status != "succeeded":
        _save(Eval, target, status="failed", error=error)


register("eval.run", [Stage("evaluate", stage_eval)], on_finish=_eval_done)
