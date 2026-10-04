"""Compute pools: one interface over HyperPod instance groups and EKS managed node groups.

A pool is addressed by `(provider, name)`; names are unique per cluster across both
providers. Workloads select a pool through `node_selector()`, capacity is driven through
`ensure()` / `scale()`, and `state()` reports schedulable nodes plus the latest reason
capacity is missing.
"""

from __future__ import annotations

from typing import Any

from ..catalog import instances
from ..core import k8s
from ..core.errors import AppError, Conflict
from . import hyperpod as hp
from . import nodegroups as ng

HYPERPOD = "hyperpod"
EC2 = "ec2"
PROVIDERS = (HYPERPOD, EC2)
HP_LABEL = "sagemaker.amazonaws.com/instance-group-name"
SYSTEM_SELECTOR = {HP_LABEL: hp.SYSTEM_GROUP}


def node_selector(provider: str, name: str) -> dict[str, str]:
    if provider == EC2:
        return {ng.POOL_LABEL: name}
    if name == hp.SYSTEM_GROUP:
        return dict(SYSTEM_SELECTOR)
    return {HP_LABEL: name, "sagemaker.amazonaws.com/node-health-status": "Schedulable"}


def find(c: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Locate a pool by name: {"provider", "instance_type", ...raw}."""
    if name == hp.SYSTEM_GROUP:
        return {"provider": HYPERPOD, "instance_type": hp.SYSTEM_INSTANCE, "name": name}
    d = hp.describe_cluster(c["region"], c["hyperpod_name"])
    g = hp.raw_group(d, name)
    if g:
        return {
            "provider": HYPERPOD,
            "name": name,
            "instance_type": g["InstanceType"].removeprefix("ml."),
            "raw": g,
            "hp": d,
        }
    n = ng.describe(c["region"], c["eks_name"], name)
    if n:
        return {
            "provider": EC2,
            "name": name,
            "instance_type": (n.get("instanceTypes") or [""])[0],
            "efa": (n.get("tags") or {}).get(ng.TAG_EFA) == "true",
            "capacity": "spot" if n.get("capacityType") == "SPOT" else "on_demand",
            "raw": n,
        }
    return None


def check_name_free(c: dict[str, Any], name: str, provider: str) -> None:
    p = find(c, name)
    if p and p["provider"] != provider:
        raise Conflict(
            "pool.name_taken",
            f"{name} is already a {p['provider']} pool on this cluster",
            detail={"name": name, "provider": p["provider"]},
        )


def ensure(
    c: dict[str, Any],
    *,
    provider: str,
    name: str,
    instance_type: str,
    count: int,
    capacity: str,
    training_plan_arn: str | None = None,
    efa: bool = False,
) -> str:
    """Create the pool or raise it to `count` nodes. Returns a log line."""
    check_name_free(c, name, provider)
    if provider == HYPERPOD:
        d = hp.describe_cluster(c["region"], c["hyperpod_name"])
        g = hp.raw_group(d, name)
        hp.check_existing_capacity(g, capacity, training_plan_arn)
        target = (g or {}).get("TargetCount", (g or {}).get("InstanceCount", 0)) if g else 0
        if g and target >= count:
            return f"{name} already targets {target} nodes"
        spec = hp.group_spec(
            d,
            name=name,
            instance_type=instance_type,
            count=count,
            capacity=capacity,
            training_plan_arn=training_plan_arn,
            existing=g,
        )
        hp.update_group(c["region"], c["hyperpod_name"], spec)
        return f"HyperPod {name} {target} → {count}"
    region, eks = c["region"], c["eks_name"]
    n = ng.describe(region, eks, name)
    if not n:
        out = ng.create(
            region,
            eks,
            cluster_id=c["id"],
            cluster_name=c["name"],
            network=c["network"],
            name=name,
            instance_type=instance_type,
            capacity=capacity,
            count=count,
            max_size=count,
            efa=efa,
        )
        return (
            f"created EC2 node group {name} ({capacity}, {instance_type}); "
            f"scaling to {out['pending_count']} once it is ACTIVE"
        )
    have = "spot" if n.get("capacityType") == "SPOT" else "on_demand"
    if have != capacity:
        raise AppError(
            "cluster.group_capacity_mismatch",
            f"node group {name} uses {have}; pick another name",
            detail={"group": name, "capacity": have},
        )
    if (n.get("instanceTypes") or [""])[0] != instance_type.removeprefix("ml."):
        raise AppError(
            "run.group_type_mismatch",
            f"node group {name} is {(n.get('instanceTypes') or ['?'])[0]}",
        )
    if efa and (n.get("tags") or {}).get(ng.TAG_EFA) != "true":
        raise AppError("nodegroup.no_efa", f"node group {name} was created without EFA")
    if n.get("status") not in ("ACTIVE", "UPDATING", "CREATING"):
        raise AppError("nodegroup.not_active", f"node group {name} is {n.get('status')}")
    target = (n.get("scalingConfig") or {}).get("desiredSize", 0)
    if target >= count:
        return f"{name} already targets {target} nodes"
    if n.get("status") == "CREATING":
        return f"{name} is still being created"
    ng.scale(region, eks, name, count)
    return f"EC2 node group {name} {target} → {count}"


def scale(c: dict[str, Any], provider: str, name: str, count: int) -> bool:
    """Set the node count (used for scale-to-zero). Returns True if something changed."""
    if provider == HYPERPOD:
        d = hp.describe_cluster(c["region"], c["hyperpod_name"])
        g = hp.raw_group(d, name)
        if not g or g.get("TargetCount", g.get("InstanceCount", 0)) == count:
            return False
        hp.update_group(
            c["region"],
            c["hyperpod_name"],
            hp.group_spec(d, name=name, instance_type="", count=count, existing=g),
        )
        return True
    n = ng.describe(c["region"], c["eks_name"], name)
    if not n or (n.get("scalingConfig") or {}).get("desiredSize", 0) == count:
        return False
    if n.get("status") not in ("ACTIVE", "DEGRADED"):
        return False  # an update is in flight; EKS rejects a concurrent one (retry later)
    ng.scale(c["region"], c["eks_name"], name, count)
    return True


def _ready_ec2_nodes(c: dict[str, Any], name: str) -> int:
    """Nodes that can take GPU pods: Ready and advertising nvidia.com/gpu."""
    nodes = (
        k8s.core(c["region"], c["eks_name"])
        .list_node(label_selector=f"{ng.POOL_LABEL}={name}")
        .items
    )
    ok = 0
    for n in nodes:
        ready = any(
            cond.type == "Ready" and cond.status == "True" for cond in n.status.conditions or []
        )
        gpus = int((n.status.allocatable or {}).get("nvidia.com/gpu", "0") or 0)
        ok += 1 if ready and gpus > 0 else 0
    return ok


def state(c: dict[str, Any], provider: str, name: str, want: int) -> dict[str, Any]:
    """{"current", "status", "why", "failed"} — `current` counts schedulable GPU nodes."""
    if provider == HYPERPOD:
        d = hp.describe_cluster(c["region"], c["hyperpod_name"])
        g = hp.raw_group(d, name) or {}
        cur = g.get("CurrentCount", 0)
        why = hp.latest_group_failure(c["region"], c["hyperpod_name"], name) if cur < want else None
        failed = d.get("FailureMessage") if d["ClusterStatus"] == "Failed" else None
        return {"current": cur, "status": g.get("Status"), "why": why, "failed": failed}
    n = ng.describe(c["region"], c["eks_name"], name)
    if not n:
        return {"current": 0, "status": "MISSING", "why": None, "failed": "node group not found"}
    cur = _ready_ec2_nodes(c, name)
    why = ng.latest_failure(c["region"], n) if cur < want else None
    if cur < want and not why:
        asg = ng.asg_counts(c["region"], n)
        if asg["in_service"] >= want:
            why = "instances running; waiting for the node to join and the GPU plugin"
    failed = None
    if n.get("status") in ("CREATE_FAILED", "DELETING", "DEGRADED") and cur < want:
        issues = (n.get("health") or {}).get("issues") or []
        failed = f"node group {n['status']}" + (f": {issues[0].get('message')}" if issues else "")
        if n.get("status") == "DEGRADED":
            failed = None  # degraded usually means capacity issues; keep waiting
    return {"current": cur, "status": n.get("status"), "why": why, "failed": failed}


def price_per_hour(region: str, provider: str, instance_type: str, capacity: str) -> float:
    if capacity == "training_plan":
        return 0.0  # prepaid
    if provider == EC2:
        p = ng.price(region, instance_type, capacity)
        if p is None and capacity == "spot":  # fall back to the On-Demand ceiling
            p = ng.price(region, instance_type, "on_demand")
        return p or 0.0
    return instances.on_demand_price(region, instance_type) or 0.0


def list_all(c: dict[str, Any], hp_desc: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """GPU pools of both providers for the UI (HyperPod system group excluded)."""
    out = []
    d = hp_desc or hp.describe_cluster(c["region"], c["hyperpod_name"])
    for g in d.get("InstanceGroups", []):
        if g["InstanceGroupName"] == hp.SYSTEM_GROUP:
            continue
        v = hp.group_view(g)
        out.append({**v, "provider": HYPERPOD})
    if c.get("eks_name"):
        for n in ng.list_tuningpad(c["region"], c["eks_name"]):
            out.append(ng.view(c["region"], n))
    return out
