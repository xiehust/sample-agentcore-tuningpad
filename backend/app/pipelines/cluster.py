"""Cluster lifecycle pipelines.

* cluster.create      CloudFormation quick-setup stack → discover → components
* cluster.import      discover an existing HyperPod EKS cluster → components
* cluster.components  (re)install platform components (repair button)
* cluster.scale       add / resize one instance group via UpdateCluster
* cluster.delete_group, cluster.delete
"""

from __future__ import annotations

from typing import Any

from ..core import aws
from ..core.db import session_scope
from ..core.errors import AppError, NotFound
from ..jobs.engine import Stage, StageContext, register
from ..models import Cluster
from ..services import components as comp
from ..services import hyperpod as hp
from ..services import project as proj

CFN_TIMEOUT_S = 2 * 3600
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

    c = load(ctx.target_id)
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


def stage_delete_runtimes(ctx: StageContext) -> None:
    """VPC-mode AgentCore runtimes hold ENIs in the cluster subnets: the stack cannot delete
    the subnets until they are gone."""
    from ..models import AgentRuntime
    from ..services import agents as asvc

    c = load(ctx.target_id)
    with session_scope() as s:
        rts = [
            (r.id, r.runtime_id)
            for r in s.query(AgentRuntime).filter(AgentRuntime.cluster_id == c["id"])
            if r.runtime_id and r.status != "deleted"
        ]
    for row_id, rid in rts:
        if (ctx.context.get("runtimes_deleted") or {}).get(row_id):
            continue  # deleting an already-deleted runtime returns AccessDenied, not NotFound
        asvc.delete_runtime(c["region"], rid)
        ctx.set("runtimes_deleted", {**(ctx.context.get("runtimes_deleted") or {}), row_id: True})
        ctx.log(f"deleted AgentCore runtime {rid}")
    subnets = c["network"].get("private_subnets") or []
    if subnets and c["source"] == "create":
        ec2 = aws.client("ec2", c["region"])

        def released():
            enis = ec2.describe_network_interfaces(
                Filters=[
                    {"Name": "subnet-id", "Values": subnets},
                    {"Name": "interface-type", "Values": ["agentic_ai"]},
                ]
            )["NetworkInterfaces"]
            ctx.detail(f"{len(enis)} AgentCore ENI(s) left")
            return not enis

        # AgentCore releases VPC ENIs asynchronously, sometimes hours after the runtime is gone
        ctx.wait_until(released, timeout_s=8 * 3600, interval_s=60, what="AgentCore ENI release")
    with session_scope() as s:
        for rid, _ in rts:
            r = s.get(AgentRuntime, rid)
            if r:
                r.status = "deleted"


def stage_delete_workloads(ctx: StageContext) -> None:
    c = load(ctx.target_id)
    if not c["eks_name"]:
        return
    from ..services import kube

    try:
        # Services first so the LBC releases NLBs before the controller disappears
        for svc in kube.list_objects(c["region"], c["eks_name"], "v1", "Service"):
            kube.delete(c["region"], c["eks_name"], "v1", "Service", svc["metadata"]["name"])
        kube.delete(c["region"], c["eks_name"], "v1", "Namespace", kube.NAMESPACE, namespace=None)
        ctx.sleep(60)
    except Exception as e:
        ctx.log(f"workload cleanup skipped: {e}")


def stage_delete_cluster(ctx: StageContext) -> None:
    c = load(ctx.target_id)
    region = c["region"]
    if c["source"] != "create":
        ctx.log("imported cluster: only unregistering")
        return
    ec2 = aws.client("ec2", region)
    for key in ("sg_acr", "sg_nlb"):
        sg = c["network"].get(key)
        if sg:
            try:
                for csg in c["network"].get("cluster_sgs", []):
                    perms = ec2.describe_security_groups(GroupIds=[csg])["SecurityGroups"][0][
                        "IpPermissions"
                    ]
                    drop = [
                        p
                        for p in perms
                        if any(u.get("GroupId") == sg for u in p.get("UserIdGroupPairs", []))
                    ]
                    if drop:
                        ec2.revoke_security_group_ingress(GroupId=csg, IpPermissions=drop)
            except Exception as e:
                ctx.log(f"revoke rules from cluster SG failed: {e}")
    if c["cfn_stack"] and hp.stack_status(region, c["cfn_stack"]):
        # also re-issues the delete after a DELETE_FAILED (e.g. once blocking ENIs are gone)
        aws.client("cloudformation", region).delete_stack(StackName=c["cfn_stack"])
        ctx.sleep(15)

        def gone():
            st = hp.stack_status(region, c["cfn_stack"])
            if st:
                ctx.detail(st["status"])
                if st["status"] == "DELETE_FAILED":
                    raise AppError(
                        "cluster.delete_failed",
                        hp.stack_failure_reason(region, c["cfn_stack"]) or "delete failed",
                    )
            return st is None

        ctx.wait_until(gone, timeout_s=CFN_TIMEOUT_S, interval_s=30, what="stack deletion")
    for key in ("sg_acr", "sg_nlb"):
        sg = c["network"].get(key)
        if sg:
            try:
                ec2.delete_security_group(GroupId=sg)
            except Exception as e:
                ctx.log(f"delete {sg}: {e}")


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
        Stage("cluster", stage_delete_cluster),
    ],
    on_finish=_on_deleted,
)
