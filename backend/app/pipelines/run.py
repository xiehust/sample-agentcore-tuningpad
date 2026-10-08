"""`run.train`: preflight → trainer image → capacity → submit RayJob → monitor (metrics,
cost, auto-retry from checkpoint) → finalize (optional scale-down).

The monitor stage is long-lived but idempotent: after a backend restart it
re-attaches to the RayJob recorded on the run (it never submits twice).
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..catalog import instances
from ..core.db import session_scope, utcnow
from ..core.errors import AppError, NotFound
from ..jobs.engine import JobCancelled, Stage, StageContext, register
from ..metrics import verl_log
from ..models import AgentRuntime, Dataset, Run, RunMetric, TrainerImage
from ..services import hyperpod as hp
from ..services import kube, pools
from ..services import project as proj
from ..services import runs as svc
from ..templates_lib import get_template
from . import cluster as pc
from . import trainer as ptrainer

TERMINAL_JOB = {"SUCCEEDED", "FAILED", "STOPPED"}
POLL_S = 30
# Driver/hardware faults a retry on the same node cannot fix (run-ba6ebd3e50: the p5's
# nvidia-fabricmanager failed, so every attempt died in CUDA init with error 802).
GPU_NODE_FAULTS = re.compile(
    r"Error 802: system not yet initialized|uncorrectable ECC error|GPU is lost"
    r"|fallen off the bus"
)
ATTEMPT_MARK = "[tuningpad] starting verl main_ppo"  # train script, once per attempt


def load_run(run_id: str) -> dict[str, Any]:
    with session_scope() as s:
        r = s.get(Run, run_id)
        if not r:
            raise NotFound("run.not_found", f"run {run_id} not found")
        return {c.name: getattr(r, c.name) for c in Run.__table__.columns}


def save_run(run_id: str, **fields: Any) -> None:
    with session_scope() as s:
        r = s.get(Run, run_id)
        if r:
            for k, v in fields.items():
                if k == "progress" and isinstance(v, dict):
                    v = {**(r.progress or {}), **v}
                setattr(r, k, v)


def _context(run: dict[str, Any]) -> dict[str, Any]:
    c = pc.load(run["cluster_id"])
    with session_scope() as s:
        rt = s.get(AgentRuntime, run["agent_runtime_id"])
        tr = s.get(Dataset, run["train_dataset_id"])
        va = s.get(Dataset, run["val_dataset_id"]) if run["val_dataset_id"] else None
        agent_template = None
        if rt:
            from ..models import Agent

            a = s.get(Agent, rt.agent_id)
            agent_template = a.template_id if a else None
        res = proj.require_region(c["region"])
        bucket = res["bucket"]
        val_split = run["spec"].get("val_split", "val")
        data = {"train": tr.splits["train"]["s3_uri"] if tr and "train" in tr.splits else None}
        if va and val_split in va.splits:
            data["val"] = va.splits[val_split]["s3_uri"]
        elif tr and "val" in tr.splits and not va:
            data["val"] = tr.splits["val"]["s3_uri"]
        return {
            "cluster": c,
            "runtime_arn": rt.runtime_arn if rt else None,
            "runtime_status": rt.status if rt else None,
            "bucket": bucket,
            "datasets": data,
            "template_id": agent_template,
        }


# ---------------- stages ----------------


def stage_preflight(ctx: StageContext) -> None:
    run = load_run(ctx.target_id)
    c = _context(run)
    if c["cluster"]["status"] != "ready":
        raise AppError("run.cluster_not_ready", f"cluster is {c['cluster']['status']}")
    if c["runtime_status"] != "ready" or not c["runtime_arn"]:
        raise AppError(
            "run.runtime_not_ready",
            "deploy the agent to this cluster (VPC runtime) and pass the smoke test first",
        )
    if not c["datasets"]["train"]:
        raise AppError("run.no_train_data", "training dataset has no train split")
    spec = instances.spec(run["compute"]["instance_type"])
    nodes = int(run["compute"]["nodes"])
    if nodes > 1 and not spec.multi_node:
        raise AppError("run.no_multinode", f"{spec.type} does not support multi-node EFA training")
    save_run(run["id"], status="preparing", started_at=run["started_at"] or utcnow())
    ctx.log(f"cluster {c['cluster']['name']} · {nodes}×{spec.type} · data {c['datasets']}")


def stage_image(ctx: StageContext) -> None:
    run = load_run(ctx.target_id)
    region = pc.load(run["cluster_id"])["region"]
    tid, ready = ptrainer.find_or_create(region, run["spec"]["plan"]["profile"])
    ctx.set("trainer_image_id", tid)

    def probe():
        with session_scope() as s:
            t = s.get(TrainerImage, tid)
            ctx.detail(f"trainer image {t.status}")
            if t.status == "failed":
                raise AppError(
                    "run.image_failed", "trainer image build failed (see Settings → images)"
                )
            if t.status == "ready":
                return t.image_uri
        return None

    uri = (
        ready
        and probe()
        or ctx.wait_until(probe, timeout_s=6 * 3600, interval_s=30, what="trainer image")
    )
    ctx.set("image", uri)
    ctx.log(f"trainer image {uri}")


def _provider(run: dict[str, Any]) -> str:
    return run["compute"].get("provider") or pools.HYPERPOD


def _capacity_ec2(ctx: StageContext, run: dict[str, Any], c: dict[str, Any]) -> None:
    group, nodes = run["compute"]["instance_group"], int(run["compute"]["nodes"])
    capacity = run["compute"].get("capacity", "on_demand")
    itype = run["compute"]["instance_type"]

    def ensure() -> None:
        ctx.log(
            pools.ensure(
                c,
                provider=pools.EC2,
                name=group,
                instance_type=itype,
                count=nodes,
                capacity=capacity,
                efa=nodes > 1,
            )
        )

    st = pools.state(c, pools.EC2, group, nodes) if pools.find(c, group) else {"current": 0}
    if st["current"] < nodes:
        if not run["compute"].get("scale_up", True):
            raise AppError("run.no_capacity", f"node group {group} has {st['current']} nodes")
        ensure()
        try:
            pc.refresh_guardian(c["id"])
        except Exception as e:
            ctx.log(f"guardian refresh skipped: {e}")
    save_run(run["id"], status="waiting_capacity")
    seen: dict[str, str] = {}

    def ready():
        sc = pools.state(c, pools.EC2, group, nodes)
        why = sc["why"]
        ctx.detail(
            f"{sc['current']}/{nodes} GPU nodes · {sc['status']}" + (f" · {why}" if why else "")
        )
        if why and why != seen.get("why"):
            ctx.log(f"provisioning: {why} (EC2 Auto Scaling keeps retrying)")
            seen["why"] = why
        if sc["failed"]:
            raise AppError("run.capacity_failed", sc["failed"])
        if sc["status"] == "ACTIVE" and sc["current"] < nodes:
            # a node group that was still CREATING could not take the scale-up yet
            p = pools.find(c, group)
            if p and (p["raw"].get("scalingConfig") or {}).get("desiredSize", 0) < nodes:
                ensure()
        return sc["current"] >= nodes

    ctx.wait_until(
        ready,
        timeout_s=int(run["compute"].get("capacity_timeout_s", 4 * 3600)),
        interval_s=30,
        what=f"{nodes} × {itype} (EC2 {capacity})",
    )


def stage_capacity(ctx: StageContext) -> None:
    run = load_run(ctx.target_id)
    c = pc.load(run["cluster_id"])
    if _provider(run) == pools.EC2:
        return _capacity_ec2(ctx, run, c)
    group, nodes = run["compute"]["instance_group"], int(run["compute"]["nodes"])
    d = hp.describe_cluster(c["region"], c["hyperpod_name"])
    g = hp.raw_group(d, group)
    target = (g or {}).get("TargetCount", (g or {}).get("InstanceCount", 0)) if g else 0
    if g and g["InstanceType"].removeprefix("ml.") != run["compute"]["instance_type"]:
        raise AppError(
            "run.group_type_mismatch",
            f"group {group} is {g['InstanceType']}, run needs {run['compute']['instance_type']}",
        )
    hp.check_existing_capacity(
        g, run["compute"].get("capacity", "on_demand"), run["compute"].get("training_plan_arn")
    )
    if target < nodes:
        if not run["compute"].get("scale_up", True):
            raise AppError(
                "run.no_capacity", f"group {group} has {target} nodes, run needs {nodes}"
            )
        spec = hp.group_spec(
            d,
            name=group,
            instance_type=run["compute"]["instance_type"],
            count=nodes,
            capacity=run["compute"].get("capacity", "on_demand"),
            training_plan_arn=run["compute"].get("training_plan_arn"),
            existing=g,
        )
        hp.update_group(c["region"], c["hyperpod_name"], spec)
        ctx.log(f"scaling {group} {target} → {nodes}")
        try:
            pc.refresh_guardian(c["id"])
        except Exception as e:
            ctx.log(f"guardian refresh skipped: {e}")
    save_run(run["id"], status="waiting_capacity")
    seen: dict[str, str] = {}

    def ready():
        dd = hp.describe_cluster(c["region"], c["hyperpod_name"])
        gg = hp.raw_group(dd, group) or {}
        cur = gg.get("CurrentCount", 0)
        why = (
            hp.latest_group_failure(c["region"], c["hyperpod_name"], group) if cur < nodes else None
        )
        ctx.detail(f"{cur}/{nodes} nodes · {gg.get('Status')}" + (f" · {why}" if why else ""))
        if why and why != seen.get("why"):
            ctx.log(f"provisioning: {why} (HyperPod keeps retrying)")
            seen["why"] = why
        if dd["ClusterStatus"] == "Failed":
            raise AppError("run.capacity_failed", dd.get("FailureMessage") or "cluster failed")
        return cur >= nodes

    ctx.wait_until(
        ready,
        timeout_s=int(run["compute"].get("capacity_timeout_s", 4 * 3600)),
        interval_s=30,
        what=f"{nodes} × {run['compute']['instance_type']}",
    )


def _submit(ctx: StageContext, run: dict[str, Any], attempt: int) -> str:
    c = _context(run)
    tpl = get_template(c["template_id"]) if c["template_id"] else {}
    objs = svc.render(
        run,
        cluster=c["cluster"],
        runtime_arn=c["runtime_arn"],
        bucket=c["bucket"],
        image=ctx.context["image"],
        template_loop=tpl.get("agent_loop") or {},
        datasets=c["datasets"],
        attempt=attempt,
    )
    region, eks = c["cluster"]["region"], c["cluster"]["eks_name"]
    kube.apply(region, eks, objs["config_map"])
    name = objs["rayjob"]["metadata"]["name"]
    if not kube.get(region, eks, svc.RAYJOB_API, "RayJob", name):
        kube.apply(region, eks, objs["rayjob"])
    save_run(
        run["id"],
        rayjob_name=name,
        status="running",
        progress={"hydra": objs["hydra"], "attempt": attempt},
    )
    ctx.log(f"submitted RayJob {name}")
    return name


def stage_submit(ctx: StageContext) -> None:
    run = load_run(ctx.target_id)
    if run["rayjob_name"] and ctx.payload.get("resume") is not True:
        ctx.log(f"RayJob {run['rayjob_name']} already submitted")
        return
    attempt = int((run["progress"] or {}).get("attempt", 0)) + (1 if run["rayjob_name"] else 0)
    _submit(ctx, run, attempt)


def _ingest_metrics(run: dict[str, Any], region: str, bucket: str) -> dict[str, Any]:
    prog = run["progress"] or {}
    offset, carry = int(prog.get("log_offset", 0)), prog.get("log_carry", "")
    text, new_offset = svc.read_log(region, bucket, run["id"], offset)
    if not text:
        return {}
    recs, carry = verl_log.parse_chunk(carry, text)
    with session_scope() as s:
        for step, metrics in recs:
            for k, v in metrics.items():
                existing = s.query(RunMetric).filter_by(run_id=run["id"], step=step, key=k).first()
                if existing:
                    existing.value = v
                else:
                    s.add(RunMetric(run_id=run["id"], step=step, key=k, value=v))
    upd: dict[str, Any] = {"log_offset": new_offset, "log_carry": carry[-20000:]}
    if recs:
        upd["step"] = max(r[0] for r in recs)
    return upd


def _cost(run: dict[str, Any], c: dict[str, Any]) -> tuple[dict[str, float], float]:
    """Accrue cost since the last poll for the pool's billed instances (≤ the run's nodes).

    Wall-clock × requested nodes over-counted waits with no instance at all (capacity
    timeouts, Spot reclaims). Returns the totals and the new accounting timestamp; the
    first call only sets the baseline.
    """
    now = time.time()
    last = (run["progress"] or {}).get("cost_at")
    totals = {"node_hours": run["node_hours"] or 0.0, "est_cost_usd": run["est_cost_usd"] or 0.0}
    if last is None:
        return totals, now
    nodes = int(run["compute"]["nodes"])
    try:
        billed = min(nodes, pools.billed_nodes(c, _provider(run), run["compute"]["instance_group"]))
    except Exception:
        billed = nodes  # unknown: bill the request so the budget guard never under-counts
    node_h = max(0.0, now - float(last)) / 3600 * billed
    price = pools.price_per_hour(
        c["region"],
        _provider(run),
        run["compute"]["instance_type"],
        run["compute"].get("capacity", "on_demand"),
    )
    return {
        "node_hours": totals["node_hours"] + node_h,
        "est_cost_usd": totals["est_cost_usd"] + node_h * price,
    }, now


def _faulty_nodes(log_tail: str, pods: list[dict[str, Any]]) -> set[str]:
    """Nodes of the latest attempt that hit a GPU node fault. Ray tags lines from other
    nodes with `ip=`; untagged lines come from the head (driver) node."""
    text = log_tail.rsplit(ATTEMPT_MARK, 1)[-1]
    by_ip = {p["ip"]: p["node"] for p in pods if p.get("ip") and p.get("node")}
    head = next((p["node"] for p in pods if p.get("type") == "head" and p.get("node")), None)
    out: set[str] = set()
    for line in text.splitlines():
        if not GPU_NODE_FAULTS.search(line):
            continue
        m = re.search(r"\bip=(\d+\.\d+\.\d+\.\d+)", line)
        node = by_ip.get(m.group(1)) if m else head
        if node:
            out.add(node)
    return out


def _replace_faulty_nodes(
    ctx: StageContext, run: dict[str, Any], c: dict[str, Any], rayjob: str, bucket: str
) -> None:
    """Before a retry: swap out nodes whose GPUs failed, so the next attempt does not land
    on the same broken hardware. Must run while the failed attempt's pods still exist."""
    region, eks = c["region"], c["eks_name"]
    offset = int((run["progress"] or {}).get("log_offset", 0))
    tail, _ = svc.read_log(region, bucket, run["id"], max(0, offset - 256 * 1024))
    pods = [p for p in svc.run_pods(region, eks, run["id"]) if p["name"].startswith(rayjob)]
    for node in sorted(_faulty_nodes(tail, pods)):
        msg = pools.replace_node(c, _provider(run), run["compute"]["instance_group"], node)
        ctx.log(f"GPU node fault on {node}: {msg}")


def stage_monitor(ctx: StageContext) -> None:
    run = load_run(ctx.target_id)
    c = pc.load(run["cluster_id"])
    region, eks = c["region"], c["eks_name"]
    bucket = proj.require_region(region)["bucket"]
    max_retries = int(run["compute"].get("max_retries", 2))
    budget = run["compute"].get("budget_usd")
    try:
        while True:
            run = load_run(ctx.target_id)
            name = run["rayjob_name"]
            st = svc.rayjob_state(region, eks, name) if name else None
            prog = _ingest_metrics(run, region, bucket)
            cost, prog["cost_at"] = _cost(run, c)
            prog["rayjob"] = st
            save_run(run["id"], progress=prog, **cost)
            step = (load_run(run["id"])["progress"] or {}).get("step")
            ctx.detail(
                f"{(st or {}).get('job') or (st or {}).get('deployment') or 'pending'}"
                f" · step {step if step is not None else '—'}"
                f" · ${cost.get('est_cost_usd', 0):.0f}"
            )
            if budget and cost.get("est_cost_usd", 0) >= float(budget):
                kube.delete(region, eks, svc.RAYJOB_API, "RayJob", name)
                raise AppError("run.budget_exceeded", f"run budget ${budget} reached; stopped")
            job_status = (st or {}).get("job")
            deploy = (st or {}).get("deployment")
            if job_status == "SUCCEEDED" or (deploy == "Complete" and job_status != "FAILED"):
                save_run(run["id"], status="succeeded")
                break
            if job_status in ("FAILED", "STOPPED") or deploy == "Failed" or (st is None and name):
                if run["retries"] >= max_retries:
                    raise AppError(
                        "run.failed",
                        f"RayJob {name} {job_status or deploy or 'missing'}: "
                        f"{(st or {}).get('message') or 'see the training log'}",
                    )
                ctx.log(
                    f"RayJob {name} {job_status or deploy or 'missing'} — retry "
                    f"{run['retries'] + 1}/{max_retries} from the latest checkpoint"
                )
                try:
                    _replace_faulty_nodes(ctx, run, c, name, bucket)
                except Exception as e:  # never block the retry itself
                    ctx.log(f"GPU node fault check skipped: {e}")
                kube.delete(region, eks, svc.RAYJOB_API, "RayJob", name)
                save_run(run["id"], retries=run["retries"] + 1, status="retrying")
                ctx.sleep(30)
                _submit(
                    ctx, load_run(run["id"]), int((run["progress"] or {}).get("attempt", 0)) + 1
                )
            ctx.sleep(POLL_S)
    except JobCancelled:
        run = load_run(ctx.target_id)
        if run["rayjob_name"]:
            kube.delete(region, eks, svc.RAYJOB_API, "RayJob", run["rayjob_name"])
        cost, _ = _cost(run, c)
        save_run(run["id"], status="stopped", ended_at=utcnow(), progress={"cost_at": None}, **cost)
        _ingest_metrics(load_run(run["id"]), region, bucket)
        raise


def stage_finalize(ctx: StageContext) -> None:
    run = load_run(ctx.target_id)
    c = pc.load(run["cluster_id"])
    bucket = proj.require_region(c["region"])["bucket"]
    time.sleep(1)
    prog = _ingest_metrics(run, c["region"], bucket)
    prog["ckpt_steps"] = svc.ckpt_steps(c["region"], bucket, run["id"])
    cost, _ = _cost(run, c)
    prog["cost_at"] = None  # a later resume starts a fresh accounting window
    save_run(run["id"], progress=prog, ended_at=utcnow(), **cost)
    _release_rayjob(c, run, ctx.log)
    if run["compute"].get("scale_down_after"):
        scale_down_if_idle(c, run["compute"]["instance_group"], ctx.log, _provider(run))


def _release_rayjob(c: dict[str, Any], run: dict[str, Any], log) -> None:
    """Delete the finished RayJob now. KubeRay otherwise keeps its RayCluster for
    ttlSecondsAfterFinished (600 s): the node group drain then waits on those pods, and the
    operator re-creates evicted workers, so GPU nodes bill ~10 more minutes. Logs and
    metrics are already in S3 / the DB, checkpoints on FSx."""
    name = run.get("rayjob_name")
    if not name or not c.get("eks_name"):
        return
    try:
        kube.delete(c["region"], c["eks_name"], svc.RAYJOB_API, "RayJob", name)
        log(f"released RayJob {name}")
    except Exception as e:  # ttlSecondsAfterFinished is the backstop
        log(f"RayJob {name} cleanup skipped: {e}")


def scale_down_if_idle(c: dict[str, Any], group: str, log, provider: str = pools.HYPERPOD) -> None:
    with session_scope() as s:
        busy = (
            s.query(Run)
            .filter(
                Run.cluster_id == c["id"],
                Run.status.in_(["running", "retrying", "preparing", "waiting_capacity"]),
            )
            .count()
        )
    if busy:
        log("other runs active on the cluster; leaving capacity (guardian handles idle)")
        return
    if provider == pools.EC2:
        if pools.scale(c, pools.EC2, group, 0):
            log(f"scaled node group {group} to 0")
        return
    d = hp.describe_cluster(c["region"], c["hyperpod_name"])
    g = hp.raw_group(d, group)
    if g and g.get("TargetCount", g.get("InstanceCount", 0)) > 0:
        hp.update_group(
            c["region"],
            c["hyperpod_name"],
            hp.group_spec(d, name=group, instance_type="", count=0, existing=g),
        )
        log(f"scaled {group} to 0")


def _finished(target: str, status: str, error: str | None) -> None:
    run = load_run(target)
    if status == "succeeded":
        save_run(target, status="succeeded", ended_at=run["ended_at"] or utcnow())
    elif status == "cancelled":
        save_run(target, status="stopped", ended_at=utcnow())
    else:
        save_run(target, status="failed", error=error, ended_at=utcnow())
    if status != "succeeded" and run["compute"].get("scale_down_after"):
        # never leave a pending scale-up behind (e.g. Spot fulfilled after we gave up)
        try:
            c = pc.load(run["cluster_id"])
            if status != "cancelled":  # stop already deleted it; failed runs keep nothing
                _release_rayjob(c, run, lambda m: None)
            scale_down_if_idle(
                c,
                run["compute"]["instance_group"],
                lambda m: None,
                _provider(run),
            )
        except Exception:
            pass  # guardian still scales idle groups to 0


register(
    "run.train",
    [
        Stage("preflight", stage_preflight),
        Stage("image", stage_image),
        Stage("capacity", stage_capacity),
        Stage("submit", stage_submit),
        Stage("monitor", stage_monitor),
        Stage("finalize", stage_finalize),
    ],
    on_finish=_finished,
)
