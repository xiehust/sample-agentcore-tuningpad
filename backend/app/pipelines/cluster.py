"""Cluster lifecycle pipelines.

* cluster.create      CloudFormation quick-setup stack → discover → components
* cluster.import      discover an existing HyperPod EKS cluster → components
* cluster.components  (re)install platform components (repair button)
* cluster.scale       add / resize one instance group via UpdateCluster
* cluster.delete_group, cluster.delete
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from ..core import aws
from ..core.db import session_scope, utcnow
from ..core.errors import AppError, NotFound
from ..jobs.engine import Stage, StageContext, register
from ..models import Cluster
from ..services import components as comp
from ..services import hyperpod as hp
from ..services import project as proj

CFN_TIMEOUT_S = 2 * 3600
DELETE_POLL_S = 30
AGENTCORE_RELEASE_WARNING_S = 8 * 3600
# The normal root-stack delete owns these resources, including all HyperPod groups.
COMPUTE_RESOURCE_TYPES = {
    "AWS::SageMaker::Cluster",
    "AWS::EKS::Cluster",
    "AWS::EKS::Nodegroup",
    "AWS::EC2::Instance",
    "AWS::AutoScaling::AutoScalingGroup",
    "AWS::FSx::FileSystem",
}
COMPONENTS = [
    "access",
    "reach",
    "namespace",
    "irsa",
    "node_ecr",
    "security_groups",
    "kuberay",
    "gpu_plugins",
    "lbc",
    "fsx",
    "guardian",
]


def load(cluster_id: str) -> dict[str, Any]:
    with session_scope() as s:
        c = s.get(Cluster, cluster_id)
        if not c:
            raise NotFound("cluster.not_found", f"cluster {cluster_id} not found")
        return {
            "id": c.id,
            "name": c.name,
            "region": c.region,
            "source": c.source,
            "status": c.status,
            "cfn_stack": c.cfn_stack,
            "eks_name": c.eks_name,
            "hyperpod_name": c.hyperpod_name,
            "hyperpod_arn": c.hyperpod_arn,
            "network": dict(c.network or {}),
            "components": dict(c.components or {}),
            "params": dict(c.params or {}),
            "idle_policy": dict(c.idle_policy or {}),
        }


def save(cluster_id: str, **fields: Any) -> None:
    with session_scope() as s:
        c = s.get(Cluster, cluster_id)
        if not c:
            return
        for k, v in fields.items():
            if k in {"network", "components", "params", "idle_policy"} and isinstance(v, dict):
                v = {**(getattr(c, k) or {}), **v}
            setattr(c, k, v)


def _set_component(cluster_id: str, name: str, status: str, detail: str | None = None) -> None:
    save(cluster_id, components={name: {"status": status, "detail": detail}})


# ---------------- create ----------------


def stage_validate(ctx: StageContext) -> None:
    c = load(ctx.target_id)
    proj.require_region(c["region"])
    hp.validate_name(c["name"])
    existing = hp.stack_status(c["region"], hp.stack_name(c["name"]))
    if existing and existing["status"] not in ("CREATE_IN_PROGRESS", "CREATE_COMPLETE"):
        raise AppError(
            "cluster.stack_exists",
            f"stack {hp.stack_name(c['name'])} exists in state {existing['status']}",
        )
    ctx.log(f"creating {hp.stack_name(c['name'])} in {c['region']} AZs {c['params'].get('az_ids')}")


def stage_stack(ctx: StageContext) -> None:
    c = load(ctx.target_id)
    region, name = c["region"], hp.stack_name(c["name"])
    cfn = aws.client("cloudformation", region)
    if not hp.stack_status(region, name):
        params = hp.cfn_parameters(
            name=c["name"],
            az_ids=c["params"]["az_ids"],
            vpc_cidr=c["params"].get("vpc_cidr", "10.192.0.0/16"),
            fsx_capacity_gib=int(c["params"].get("fsx_capacity_gib", 1200)),
            tags=aws.tag_map({"TuningPadCluster": c["id"]}),
        )
        cfn.create_stack(
            StackName=name,
            TemplateURL=hp.template_url(region),
            Parameters=params,
            Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
            OnFailure="DO_NOTHING",
            Tags=aws.tags({"TuningPadCluster": c["id"]}),
        )
        ctx.log(f"create-stack {name} submitted")
    save(ctx.target_id, cfn_stack=name, status="creating")

    def probe():
        st = hp.stack_status(region, name)
        if not st:
            return None
        ctx.detail(hp.stack_progress(region, name) or st["status"])
        if st["status"] == "CREATE_COMPLETE":
            return st
        if st["status"].endswith("FAILED") or "ROLLBACK" in st["status"]:
            reason = hp.stack_failure_reason(region, name) or st["reason"]
            raise AppError("cluster.stack_failed", f"stack {st['status']}: {reason}")
        return None

    st = ctx.wait_until(probe, timeout_s=CFN_TIMEOUT_S, interval_s=30, what="CloudFormation stack")
    out = st["outputs"]
    save(
        ctx.target_id,
        hyperpod_name=out.get("OutputHyperPodClusterName"),
        hyperpod_arn=out.get("OutputHyperPodClusterArn"),
        eks_name=out.get("OutputEKSClusterName"),
    )
    ctx.log(f"stack complete: {out}")


def stage_discover(ctx: StageContext) -> None:
    c = load(ctx.target_id)
    name = c["hyperpod_name"]
    if not name:
        raise AppError("cluster.no_hyperpod", "HyperPod cluster name unknown")

    def ready():
        d = hp.describe_cluster(c["region"], name)
        ctx.detail(f"HyperPod {d['ClusterStatus']}")
        if d["ClusterStatus"] == "Failed":
            raise AppError("cluster.hyperpod_failed", d.get("FailureMessage") or "cluster failed")
        return d["ClusterStatus"] == "InService"

    ctx.wait_until(ready, timeout_s=3600, interval_s=20, what="HyperPod InService")
    info = hp.discover(c["region"], name)
    save(
        ctx.target_id,
        hyperpod_arn=info["hyperpod_arn"],
        eks_name=info["eks_name"],
        network=info["network"],
        params={
            "eks_version": info["eks_version"],
            "auth_mode": info["auth_mode"],
            "node_provisioning_mode": info["node_provisioning_mode"],
        },
    )
    if info["auth_mode"] not in ("API", "API_AND_CONFIG_MAP"):
        raise AppError(
            "cluster.auth_mode",
            f"EKS authentication mode {info['auth_mode']} does not support access entries",
        )
    ctx.log(f"discovered EKS {info['eks_name']} {info['eks_version']}, network {info['network']}")


# ---------------- components ----------------


def _exec_roles(c: dict[str, Any]) -> list[str]:
    d = hp.describe_cluster(c["region"], c["hyperpod_name"])
    return sorted(
        {g["ExecutionRole"] for g in d.get("InstanceGroups", []) if g.get("ExecutionRole")}
    )


def _component(name: str, fn):
    def run(ctx: StageContext) -> None:
        _set_component(ctx.target_id, name, "installing")
        try:
            detail = fn(ctx, load(ctx.target_id))
        except Exception as e:
            _set_component(ctx.target_id, name, "failed", str(e)[:300])
            raise
        _set_component(ctx.target_id, name, "ready", detail if isinstance(detail, str) else None)
        ctx.log(f"{name}: {detail}")

    return Stage(f"component.{name}", run)


def _c_access(ctx, c):
    return comp.ensure_access_entry(c["region"], c["eks_name"])


def _c_reach(ctx, c):
    # a fresh access entry takes a few seconds to propagate
    last: list[str] = []

    def probe():
        try:
            return comp.check_reach(c["region"], c["eks_name"])
        except Exception as e:
            last[:] = [f"{type(e).__name__}: {e}"]
            ctx.detail(f"waiting for API access: {last[0][:120]}")
            return None

    return ctx.wait_until(probe, timeout_s=180, interval_s=10, what="Kubernetes API") or last


def _c_namespace(ctx, c):
    return comp.ensure_namespace(c["region"], c["eks_name"])


def _c_irsa(ctx, c):
    roles = comp.ensure_irsa_roles(
        c["region"], c["name"], c["eks_name"], c["hyperpod_arn"], _exec_roles(c)
    )
    save(ctx.target_id, params={"roles": roles})
    return f"IRSA roles: {', '.join(roles)}"


def _c_node_ecr(ctx, c):
    return comp.ensure_node_ecr_pull(c["region"], _exec_roles(c))


def _c_sgs(ctx, c):
    sgs = comp.ensure_security_groups(c["region"], c["name"], c["network"])
    save(ctx.target_id, network=sgs)
    return f"sg-acr {sgs['sg_acr']}, sg-nlb {sgs['sg_nlb']}"


def _c_kuberay(ctx, c):
    return comp.install_kuberay(c["region"], c["eks_name"])


def _c_gpu_plugins(ctx, c):
    from ..services import nodegroups as ngs

    ngs.ensure_node_role(c["region"], c["name"])  # ready before the first node group
    return comp.install_gpu_plugins(c["region"], c["eks_name"])


def _c_lbc(ctx, c):
    return comp.install_lbc(
        c["region"], c["eks_name"], c["network"]["vpc_id"], c["params"]["roles"]["lbc"]
    )


def _c_fsx(ctx, c):
    return comp.ensure_fsx_pvc(c["region"], c["eks_name"], c["network"], c["name"])


def _c_guardian(ctx, c):
    return comp.install_guardian(c["region"], c["eks_name"], guardian_config_for(c))


def guardian_config_for(c: dict[str, Any]) -> dict[str, Any]:
    from ..catalog import instances

    res = proj.require_region(c["region"])
    policy = c.get("idle_policy") or {}
    groups = {}
    try:
        d = hp.describe_cluster(c["region"], c["hyperpod_name"])
        raw = d.get("InstanceGroups", [])
    except Exception:
        raw = []
    for g in raw:
        name = g["InstanceGroupName"]
        if name == hp.SYSTEM_GROUP:
            continue
        price = None
        try:  # training-plan groups are prepaid: no running cost toward the budget
            if not g.get("TrainingPlanArn"):
                price = instances.on_demand_price(c["region"], g["InstanceType"])
        except Exception:
            pass
        gp = (policy.get("groups") or {}).get(name, {})
        groups[name] = {
            "idle_minutes": int(gp.get("idle_minutes", policy.get("idle_minutes", 30))),
            "managed": bool(gp.get("managed", True)),
            "price_per_hour": price or 0,
        }
    nodegroups = {}
    if c.get("eks_name"):
        from ..services import nodegroups as ngs

        try:
            found = ngs.list_tuningpad(c["region"], c["eks_name"])
        except Exception:
            found = []
        for n in found:
            name = n["nodegroupName"]
            gp = (policy.get("groups") or {}).get(name, {})
            cap = "spot" if n.get("capacityType") == "SPOT" else "on_demand"
            itype = (n.get("instanceTypes") or [""])[0]
            price = ngs.price(c["region"], itype, cap)
            if price is None:  # Spot price unknown: account at the On-Demand ceiling
                price = ngs.price(c["region"], itype, "on_demand")
            nodegroups[name] = {
                "idle_minutes": int(gp.get("idle_minutes", policy.get("idle_minutes", 30))),
                "managed": bool(gp.get("managed", True)),
                "price_per_hour": price or 0,
            }
    cfg = comp.guardian_config(
        c["region"], c["hyperpod_name"], c["id"], res["bucket"], groups, policy.get("budget_usd")
    )
    cfg["eks_cluster"] = c.get("eks_name")
    cfg["nodegroups"] = nodegroups
    return cfg


def refresh_guardian(cluster_id: str) -> None:
    c = load(cluster_id)
    if (c["components"].get("guardian") or {}).get("status") == "ready":
        comp.install_guardian(c["region"], c["eks_name"], guardian_config_for(c))


COMPONENT_STAGES = [
    _component("access", _c_access),
    _component("reach", _c_reach),
    _component("namespace", _c_namespace),
    _component("irsa", _c_irsa),
    _component("node_ecr", _c_node_ecr),
    _component("security_groups", _c_sgs),
    _component("kuberay", _c_kuberay),
    _component("gpu_plugins", _c_gpu_plugins),
    _component("lbc", _c_lbc),
    _component("fsx", _c_fsx),
    _component("guardian", _c_guardian),
]


def _on_ready(target: str, status: str, error: str | None) -> None:
    if status == "succeeded":
        save(target, status="ready")
    elif status == "failed":
        save(target, status="degraded" if load(target)["hyperpod_arn"] else "failed")


register(
    "cluster.create",
    [
        Stage("validate", stage_validate),
        Stage("stack", stage_stack),
        Stage("discover", stage_discover),
        *COMPONENT_STAGES,
    ],
    on_finish=_on_ready,
)
register(
    "cluster.import", [Stage("discover", stage_discover), *COMPONENT_STAGES], on_finish=_on_ready
)
register("cluster.components", COMPONENT_STAGES, on_finish=_on_ready)


# ---------------- scale / groups ----------------


def stage_scale(ctx: StageContext) -> None:
    c = load(ctx.target_id)
    p = ctx.payload
    d = hp.describe_cluster(c["region"], c["hyperpod_name"])
    existing = hp.raw_group(d, p["group"])
    if int(p["count"]) > 0:
        hp.check_existing_capacity(
            existing, p.get("capacity", "on_demand"), p.get("training_plan_arn")
        )
    spec = hp.group_spec(
        d,
        name=p["group"],
        instance_type=p.get("instance_type") or "",
        count=int(p["count"]),
        capacity=p.get("capacity", "on_demand"),
        training_plan_arn=p.get("training_plan_arn"),
        deep_health_checks=bool(p.get("deep_health_checks")),
        existing=existing,
    )
    if existing is None and not p.get("instance_type"):
        raise AppError(
            "cluster.instance_type_required", "instance type is required for a new group"
        )
    if existing is None:
        from ..catalog import instances
        from ..services import nodegroups as ngs

        instances.spec(spec["InstanceType"])  # P-family only
        if c["eks_name"] and ngs.describe(c["region"], c["eks_name"], p["group"]):
            raise AppError(
                "pool.name_taken",
                f"{p['group']} is already an EC2 node group on this cluster",
                detail={"name": p["group"], "provider": "ec2"},
            )
    hp.update_group(c["region"], c["hyperpod_name"], spec)
    ctx.log(f"UpdateCluster {p['group']} → {spec['InstanceType']} × {spec['InstanceCount']}")
    ctx.set("spec", {k: v for k, v in spec.items() if k != "LifeCycleConfig"})


def stage_wait_scale(ctx: StageContext) -> None:
    c = load(ctx.target_id)
    want = int(ctx.payload["count"])
    timeout = int(ctx.payload.get("timeout_s", 3600))

    def probe():
        d = hp.describe_cluster(c["region"], c["hyperpod_name"])
        g = hp.raw_group(d, ctx.payload["group"]) or {}
        cur = g.get("CurrentCount", 0)
        ctx.detail(f"{g.get('Status', '?')} · {cur}/{want} nodes · cluster {d['ClusterStatus']}")
        if d["ClusterStatus"] == "Failed":
            raise AppError("cluster.update_failed", d.get("FailureMessage") or "update failed")
        return d["ClusterStatus"] == "InService" and cur == want

    ctx.wait_until(probe, timeout_s=timeout, interval_s=20, what="instance group capacity")


def stage_refresh_guardian(ctx: StageContext) -> None:
    try:
        refresh_guardian(ctx.target_id)
    except Exception as e:  # advisory: the next components run will converge
        ctx.log(f"guardian refresh skipped: {e}")


register(
    "cluster.scale",
    [
        Stage("update", stage_scale),
        Stage("wait", stage_wait_scale),
        Stage("guardian", stage_refresh_guardian),
    ],
)


def stage_delete_group(ctx: StageContext) -> None:
    c = load(ctx.target_id)
    if ctx.payload["group"] == hp.SYSTEM_GROUP:
        raise AppError("cluster.system_group", "the system instance group cannot be deleted")
    d = hp.describe_cluster(c["region"], c["hyperpod_name"])
    if hp.raw_group(d, ctx.payload["group"]):
        hp.delete_group(c["region"], c["hyperpod_name"], ctx.payload["group"])

    def gone():
        d = hp.describe_cluster(c["region"], c["hyperpod_name"])
        ctx.detail(d["ClusterStatus"])
        return hp.raw_group(d, ctx.payload["group"]) is None and d["ClusterStatus"] == "InService"

    ctx.wait_until(gone, timeout_s=3600, interval_s=20, what="instance group deletion")


register(
    "cluster.delete_group",
    [Stage("delete", stage_delete_group), Stage("guardian", stage_refresh_guardian)],
)


# ---------------- EC2 node groups ----------------


def stage_nodegroup_apply(ctx: StageContext) -> None:
    from ..services import nodegroups as ngs
    from ..services import pools

    c = load(ctx.target_id)
    p = ctx.payload
    n = ngs.describe(c["region"], c["eks_name"], p["name"])
    if n is None:
        pools.check_name_free(c, p["name"], pools.EC2)
        out = ngs.create(
            c["region"],
            c["eks_name"],
            cluster_id=c["id"],
            cluster_name=c["name"],
            network=c["network"],
            name=p["name"],
            instance_type=p["instance_type"],
            capacity=p.get("capacity", "on_demand"),
            count=int(p["count"]),
            max_size=int(p.get("max_size") or p["count"] or 1),
            efa=bool(p.get("efa")),
            az_id=p.get("az_id"),
        )
        ctx.log(f"CreateNodegroup {p['name']}: {out}")
        if out["pending_count"]:
            ctx.set("pending_count", out["pending_count"])
        return
    if n.get("status") == "CREATING":
        ctx.set("pending_count", int(p["count"]))
        return
    ngs.scale(c["region"], c["eks_name"], p["name"], int(p["count"]))
    ctx.log(f"UpdateNodegroupConfig {p['name']} desired → {p['count']}")


def stage_nodegroup_wait(ctx: StageContext) -> None:
    from ..services import nodegroups as ngs
    from ..services import pools

    c = load(ctx.target_id)
    name, want = ctx.payload["name"], int(ctx.payload["count"])
    seen: dict[str, str] = {}

    def probe():
        n = ngs.describe(c["region"], c["eks_name"], name)
        if not n:
            raise AppError("nodegroup.not_found", f"node group {name} disappeared")
        if n["status"] == "CREATE_FAILED":
            issues = (n.get("health") or {}).get("issues") or [{}]
            raise AppError("nodegroup.create_failed", issues[0].get("message") or "create failed")
        if n["status"] != "ACTIVE" and n["status"] != "DEGRADED":
            ctx.detail(n["status"])
            return False
        if ctx.context.get("pending_count") is not None:
            ngs.scale(c["region"], c["eks_name"], name, int(ctx.context["pending_count"]))
            ctx.set("pending_count", None)
            return False
        if want == 0:
            return True
        st = pools.state(c, pools.EC2, name, want)
        ctx.detail(f"{st['current']}/{want} GPU nodes" + (f" · {st['why']}" if st["why"] else ""))
        if st["why"] and st["why"] != seen.get("why"):
            ctx.log(f"provisioning: {st['why']}")
            seen["why"] = st["why"]
        return st["current"] >= want

    ctx.wait_until(
        probe,
        timeout_s=int(ctx.payload.get("timeout_s", 3600)),
        interval_s=20,
        what="EC2 node group capacity",
    )


register(
    "cluster.nodegroup",
    [
        Stage("apply", stage_nodegroup_apply),
        Stage("wait", stage_nodegroup_wait),
        Stage("guardian", stage_refresh_guardian),
    ],
)


def _delete_nodegroup(ctx: StageContext, c: dict[str, Any], name: str) -> None:
    from ..services import nodegroups as ngs

    if ngs.describe(c["region"], c["eks_name"], name):
        ngs.delete(c["region"], c["eks_name"], name)
        ctx.log(f"DeleteNodegroup {name}")

    def gone():
        n = ngs.describe(c["region"], c["eks_name"], name)
        ctx.detail(f"{name}: {n['status'] if n else 'deleted'}")
        if n and n["status"] == "DELETE_FAILED":
            raise AppError("nodegroup.delete_failed", f"node group {name} DELETE_FAILED")
        return n is None

    ctx.wait_until(gone, timeout_s=3600, interval_s=20, what=f"node group {name} deletion")
    ngs.cleanup_artifacts(c["region"], c["eks_name"], name)


def stage_delete_nodegroup(ctx: StageContext) -> None:
    _delete_nodegroup(ctx, load(ctx.target_id), ctx.payload["name"])


register(
    "cluster.delete_nodegroup",
    [Stage("delete", stage_delete_nodegroup), Stage("guardian", stage_refresh_guardian)],
)


def stage_delete_nodegroups(ctx: StageContext) -> None:
    """Before the stack goes: EKS cannot be deleted while managed node groups exist."""
    from ..services import nodegroups as ngs

    c = _load_cleanup(ctx)
    if not c["eks_name"]:
        return
    try:
        found = ngs.list_tuningpad(c["region"], c["eks_name"])
    except Exception as e:  # EKS already gone
        ctx.log(f"node groups: {e}")
        return
    for n in found:
        ngs.delete(c["region"], c["eks_name"], n["nodegroupName"])
    for n in found:
        _delete_nodegroup(ctx, c, n["nodegroupName"])
    if c["source"] == "create":
        try:
            ngs.delete_node_role(c["region"], c["name"])
        except Exception as e:
            ctx.log(f"node role cleanup: {e}")


def _cluster_runtimes(cluster_id: str) -> list[tuple[str, str]]:
    from ..models import AgentRuntime

    with session_scope() as s:
        rows = s.query(AgentRuntime).filter(AgentRuntime.cluster_id == cluster_id).all()
        runtimes = [(r.id, r.runtime_id) for r in rows if r.runtime_id and r.status != "deleted"]
        # Eval-only OTEL twins sit in the same subnets.
        runtimes += [
            (f"{r.id}:obs", r.obs["runtime_id"])
            for r in rows
            if (r.obs or {}).get("runtime_id") and r.obs.get("status") != "deleted"
        ]
    return runtimes


def _runtime_states(region: str) -> dict[str, str]:
    """List instead of Get: an already deleted runtime can return AccessDenied on Get."""
    ctl = aws.client("bedrock-agentcore-control", region)
    states: dict[str, str] = {}
    token = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = ctl.list_agent_runtimes(**kwargs)
        for runtime in page.get("agentRuntimes", []):
            states[runtime["agentRuntimeId"]] = runtime.get("status", "")
        token = page.get("nextToken")
        if not token:
            return states


def _runtime_cleanup_status(row_id: str, status: str) -> None:
    from ..models import AgentRuntime

    with session_scope() as s:
        base_id = row_id.removesuffix(":obs")
        runtime = s.get(AgentRuntime, base_id)
        if runtime:
            if row_id.endswith(":obs"):
                runtime.obs = {**(runtime.obs or {}), "status": status}
            else:
                runtime.status = status


def _start_network_wait(ctx: StageContext) -> datetime:
    started = ctx.context.get("agentcore_cleanup_started_at")
    if not started:
        started = utcnow().isoformat()
        ctx.set("agentcore_cleanup_started_at", started)
    return datetime.fromisoformat(started).replace(tzinfo=UTC)


def stage_delete_runtimes(ctx: StageContext) -> None:
    """Submit runtime deletions; release compute while AWS asynchronously removes ENIs."""
    from ..services import agents as asvc

    c = _load_cleanup(ctx)
    runtimes = _cluster_runtimes(c["id"])
    if not runtimes:
        return
    # Keep the legacy key: it means the delete was accepted, not that the runtime is gone.
    accepted = dict(ctx.context.get("runtimes_deleted") or {})
    states = _runtime_states(c["region"]) if any(not accepted.get(r) for r, _ in runtimes) else {}
    busy = 0
    for row_id, runtime_id in runtimes:
        if accepted.get(row_id):
            _start_network_wait(ctx)
            _runtime_cleanup_status(row_id, "deleting")
            continue
        state = states.get(runtime_id)
        if state is None:
            # Covers interruption between AWS accepting delete and ctx.set().
            _runtime_cleanup_status(row_id, "deleted")
        elif state == "DELETING":
            _start_network_wait(ctx)
            _runtime_cleanup_status(row_id, "deleting")
        elif asvc.runtime_busy(state):
            busy += 1
            continue
        else:
            asvc.delete_runtime(c["region"], runtime_id)
            _start_network_wait(ctx)
            _runtime_cleanup_status(row_id, "deleting")
            ctx.log(f"delete requested for AgentCore runtime {runtime_id}")
        accepted[row_id] = True
        ctx.set("runtimes_deleted", dict(accepted))
    if busy:
        ctx.defer(
            delay_s=DELETE_POLL_S,
            reason="clusterRuntimeDeleting",
            detail=f"waiting for {busy} AgentCore runtime(s) before deletion",
            detail_params={"runtime_count": busy},
        )


def stage_delete_workloads(ctx: StageContext) -> None:
    c = _load_cleanup(ctx)
    if not c["eks_name"]:
        return
    from kubernetes.client.exceptions import ApiException

    from ..services import kube

    namespace_requested = False
    try:
        services = kube.list_objects(c["region"], c["eks_name"], "v1", "Service")
        ctx.set("workload_services", [svc["metadata"]["name"] for svc in services])
        for service in services:
            if not service["metadata"].get("deletionTimestamp"):
                kube.delete(
                    c["region"], c["eks_name"], "v1", "Service", service["metadata"]["name"]
                )
        if not ctx.context.get("workload_namespace_delete_requested"):
            # Namespace termination also stops RayJob controllers recreating Services.
            # The LBC is in kube-system, so it can finish their AWS finalizers.
            kube.delete(
                c["region"], c["eks_name"], "v1", "Namespace", kube.NAMESPACE, namespace=None
            )
            ctx.set("workload_namespace_delete_requested", True)
            namespace_requested = True
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        ctx.log("EKS cluster is already absent")
        services = []
    except ApiException as error:
        if error.status != 404:
            raise
        services = []
    if services or namespace_requested:
        # LBC removes its Service finalizer only after AWS load-balancer cleanup. Keep
        # both its controller and the system node alive until that reconciliation ends.
        # Always re-list after namespace termination starts, closing the race where a
        # controller creates a Service between our initial list and namespace deletion.
        ctx.defer(
            delay_s=DELETE_POLL_S,
            reason="clusterWorkloadsDeleting",
            detail=f"waiting for {len(services)} Service(s) and load balancer cleanup",
            detail_params={"service_count": len(services)},
        )
    ctx.set("workloads_cleanup_verified", True)


def _load_cleanup(ctx: StageContext) -> dict[str, Any]:
    """A generic job retry also restores the cluster's active deletion state."""
    c = load(ctx.target_id)
    if c["status"] != "deleting":
        save(c["id"], status="deleting")
        c["status"] = "deleting"
    return c


def _delete_stack_state(ctx: StageContext, c: dict[str, Any]) -> dict[str, Any] | None:
    # A stack name can be reused. Once observed, always monitor/delete that exact ARN.
    stack_id = ctx.context.get("delete_stack_id") or c["cfn_stack"]
    if not stack_id:
        return None
    state = hp.stack_status(c["region"], stack_id)
    if state and not ctx.context.get("delete_stack_id"):
        ctx.set("delete_stack_id", state["stack_id"])
    return state


def _capture_delete_subnets(ctx: StageContext, c: dict[str, Any]) -> None:
    if ctx.context.get("delete_subnets_captured"):
        return
    resources = hp.stack_resources(c["region"], ctx.context["delete_stack_id"])
    subnets = set(c["network"].get("private_subnets") or [])
    subnets.update(
        r["PhysicalResourceId"]
        for r in resources
        if r["ResourceType"] == "AWS::EC2::Subnet" and r.get("PhysicalResourceId")
    )
    # Discovery may only have recorded HyperPod's subnet, rather than all root-stack subnets.
    ctx.set("delete_subnets", sorted(subnets))
    ctx.set("delete_subnets_captured", True)


def _revoke_platform_sg_rules(ctx: StageContext, ec2, c: dict[str, Any]) -> None:
    if ctx.context.get("platform_sg_rules_revoked"):
        return
    owned = {c["network"].get(key) for key in ("sg_acr", "sg_nlb")} - {None}
    for group_id in c["network"].get("cluster_sgs", []) if owned else []:
        try:
            groups = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"]
            drop = []
            for permission in groups[0].get("IpPermissions", []):
                pairs = [
                    p for p in permission.get("UserIdGroupPairs", []) if p.get("GroupId") in owned
                ]
                if pairs:
                    # Revoke only references to our groups, preserving other peers and CIDRs
                    # sharing the same protocol/port permission.
                    drop.append(
                        {
                            **{
                                k: v
                                for k, v in permission.items()
                                if k in {"IpProtocol", "FromPort", "ToPort"}
                            },
                            "UserIdGroupPairs": pairs,
                        }
                    )
            if drop:
                ec2.revoke_security_group_ingress(GroupId=group_id, IpPermissions=drop)
        except ClientError as error:
            if error.response["Error"]["Code"] not in {
                "InvalidGroup.NotFound",
                "InvalidPermission.NotFound",
            }:
                raise
    ctx.set("platform_sg_rules_revoked", True)


def _network_stack_failure(c: dict[str, Any], stack_id: str) -> list[dict[str, Any]]:
    resources = hp.stack_resources(c["region"], stack_id)
    failures = hp.stack_delete_failures(resources)
    if not failures or not all(hp.network_delete_dependency(r) for r in failures):
        reason = "; ".join(
            f"{r['LogicalResourceId']}: {r.get('ResourceStatusReason') or r['ResourceType']}"
            for r in failures
        )
        raise AppError(
            "cluster.delete_failed",
            reason or "stack DELETE_FAILED without a confirmed network dependency",
        )
    return resources


def _request_stack_delete(ctx: StageContext, c: dict[str, Any]) -> None:
    stack_id = ctx.context["delete_stack_id"]
    aws.client("cloudformation", c["region"]).delete_stack(StackName=stack_id)
    ctx.log(f"delete-stack {stack_id} submitted")


def _defer_stack(ctx: StageContext, state: dict[str, Any], *, compute: bool) -> None:
    ctx.defer(
        delay_s=DELETE_POLL_S,
        reason="clusterComputeDeleting" if compute else "clusterStackDeleting",
        detail=state["status"],
        detail_params={"status": state["status"]},
    )


def stage_delete_compute(ctx: StageContext) -> None:
    """Start the normal root delete before waiting for AgentCore's asynchronous ENIs.

    CloudFormation's reverse dependencies release HyperPod, EKS and FSx before their
    network. Its root delete can then stop on a known network dependency; that is a
    durable waiting state, not a failed TuningPad job.
    """
    c = _load_cleanup(ctx)
    if c["source"] != "create":
        return
    state = _delete_stack_state(ctx, c)
    if (
        state
        and not state["status"].startswith("DELETE_")
        and not ctx.context.get("workloads_cleanup_verified")
    ):
        # Old jobs can have a succeeded workloads stage from the former 60-second sleep.
        # Verify finalizers before this newly inserted stage starts their first root delete.
        stage_delete_workloads(ctx)
    if state and state["status"] != "DELETE_COMPLETE":
        _capture_delete_subnets(ctx, c)
    ec2 = aws.client("ec2", c["region"])
    _revoke_platform_sg_rules(ctx, ec2, c)
    if "platform_sgs_remaining" not in ctx.context:
        ctx.set("platform_sgs_remaining", _delete_platform_sgs(ctx, ec2, c))
    if not state or state["status"] == "DELETE_COMPLETE":
        return
    if state["status"] == "DELETE_FAILED":
        resources = _network_stack_failure(c, ctx.context["delete_stack_id"])
        compute_left = [
            r
            for r in resources
            if r["ResourceType"] in COMPUTE_RESOURCE_TYPES
            and r.get("PhysicalResourceId")
            and r["ResourceStatus"] != "DELETE_COMPLETE"
        ]
        if not compute_left:
            return
        # A different template can leave compute behind a network dependency. Keep this
        # stage waiting and retry the root once blockers clear; never claim compute is gone.
        _wait_network_release(ctx, c)
        _request_stack_delete(ctx, c)
    elif state["status"] != "DELETE_IN_PROGRESS":
        _request_stack_delete(ctx, c)
    _defer_stack(ctx, state, compute=True)


def _remaining_agentcore_enis(ctx: StageContext, c: dict[str, Any]) -> int:
    subnets = ctx.context.get("delete_subnets", c["network"].get("private_subnets") or [])
    if not subnets:
        return 0
    ec2 = aws.client("ec2", c["region"])
    interfaces: set[str] = set()
    token = None
    while True:
        kwargs: dict[str, Any] = {
            "IncludeManagedResources": True,
            "MaxResults": 1000,
            "Filters": [
                {"Name": "subnet-id", "Values": subnets},
                {"Name": "interface-type", "Values": ["agentic_ai"]},
            ],
        }
        if token:
            kwargs["NextToken"] = token
        page = ec2.describe_network_interfaces(**kwargs)
        interfaces.update(r["NetworkInterfaceId"] for r in page.get("NetworkInterfaces", []))
        token = page.get("NextToken")
        if not token:
            return len(interfaces)


def _wait_network_release(ctx: StageContext, c: dict[str, Any]) -> None:
    retry_runtimes = ctx.context.get("runtime_delete_retry") or []
    if retry_runtimes:
        # Only a prior, terminal DELETE_FAILED populates this marker. Failed jobs are
        # never scheduled automatically, so reaching here means an explicit job retry.
        # The original runtimes stage may already be succeeded and will not be replayed.
        stage_delete_runtimes(ctx)
        ctx.set("runtime_delete_retry", [])
        ctx.defer(
            delay_s=DELETE_POLL_S,
            reason="clusterRuntimeDeleting",
            detail="AgentCore runtime deletion retried; waiting for AWS to reconcile",
            detail_params={"runtime_count": len(retry_runtimes)},
        )
    runtimes = _cluster_runtimes(c["id"])
    remaining_runtimes = 0
    if runtimes:
        states = _runtime_states(c["region"])
        failed = [(row_id, rid) for row_id, rid in runtimes if states.get(rid) == "DELETE_FAILED"]
        if failed:
            accepted = dict(ctx.context.get("runtimes_deleted") or {})
            for row_id, _ in failed:
                accepted.pop(row_id, None)
                _runtime_cleanup_status(row_id, "delete_failed")
            ctx.set("runtimes_deleted", accepted)
            ctx.set("runtime_delete_retry", [row_id for row_id, _ in failed])
            raise AppError(
                "cluster.delete_failed",
                "AgentCore runtime deletion failed: " + ", ".join(rid for _, rid in failed),
            )
        for row_id, runtime_id in runtimes:
            if runtime_id in states:
                remaining_runtimes += 1
            else:
                _runtime_cleanup_status(row_id, "deleted")
    if c["source"] == "create":
        eni_count = _remaining_agentcore_enis(ctx, c)
        if eni_count:
            elapsed = max(0, (utcnow() - _start_network_wait(ctx)).total_seconds())
            delayed = elapsed >= AGENTCORE_RELEASE_WARNING_S
            if delayed and not ctx.context.get("agentcore_delay_warned"):
                ctx.log("AgentCore ENI cleanup has exceeded 8 hours; automatic checks continue")
                ctx.set("agentcore_delay_warned", True)
            ctx.defer(
                delay_s=60,
                reason="clusterAgentcoreEnisDelayed" if delayed else "clusterAgentcoreEnis",
                detail=f"{eni_count} AgentCore ENI(s) left; automatic cleanup will continue",
                detail_params={
                    "eni_count": eni_count,
                    "waited_hours": round(elapsed / 3600, 1),
                },
            )
    if remaining_runtimes:
        ctx.defer(
            delay_s=DELETE_POLL_S,
            reason="clusterRuntimeDeleting",
            detail=f"waiting for {remaining_runtimes} AgentCore runtime(s) to disappear",
            detail_params={"runtime_count": remaining_runtimes},
        )
    if c["source"] == "create":
        left = _delete_platform_sgs(
            ctx,
            aws.client("ec2", c["region"]),
            c,
            only=ctx.context.get("platform_sgs_remaining"),
        )
        ctx.set("platform_sgs_remaining", left)
        if left:
            ctx.defer(
                delay_s=60,
                reason="clusterSecurityGroups",
                detail=f"waiting for {len(left)} security group(s) to be released",
                detail_params={"group_count": len(left)},
            )


def stage_delete_network(ctx: StageContext) -> None:
    _wait_network_release(ctx, _load_cleanup(ctx))


def stage_delete_cluster(ctx: StageContext) -> None:
    c = _load_cleanup(ctx)
    if c["source"] != "create":
        ctx.log("imported cluster: only unregistering")
        return
    state = _delete_stack_state(ctx, c)
    if not state or state["status"] == "DELETE_COMPLETE":
        return
    if state["status"] == "DELETE_FAILED":
        _network_stack_failure(c, ctx.context["delete_stack_id"])
        _wait_network_release(ctx, c)
        _request_stack_delete(ctx, c)
    elif state["status"] != "DELETE_IN_PROGRESS":
        _request_stack_delete(ctx, c)
    _defer_stack(ctx, state, compute=False)


def _delete_platform_sgs(
    ctx: StageContext, ec2, c: dict, only: list[str] | None = None
) -> list[str]:
    """Delete TuningPad's own SGs (sg-acr, sg-nlb); return the ones still in use."""
    left = []
    for key in ("sg_acr", "sg_nlb"):
        sg = c["network"].get(key)
        if not sg or (only is not None and sg not in only):
            continue
        try:
            ec2.delete_security_group(GroupId=sg)
            ctx.log(f"deleted security group {sg}")
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "InvalidGroup.NotFound":
                continue
            if code != "DependencyViolation":
                raise
            ctx.log(f"security group {sg} is still in use")
            left.append(sg)
    return left


def _on_deleted(target: str, status: str, error: str | None) -> None:
    if status == "succeeded":
        with session_scope() as s:
            c = s.get(Cluster, target)
            if c:
                c.status = "deleted"
    else:
        save(target, status="delete_failed")


register(
    "cluster.delete",
    [
        Stage("workloads", stage_delete_workloads),
        Stage("nodegroups", stage_delete_nodegroups),
        Stage("runtimes", stage_delete_runtimes),
        Stage("compute", stage_delete_compute),
        Stage("network", stage_delete_network),
        Stage("cluster", stage_delete_cluster),
    ],
    on_finish=_on_deleted,
)
