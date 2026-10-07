from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..catalog import instances
from ..core import aws
from ..core.config import get_settings
from ..core.db import new_id, session_scope
from ..core.errors import AppError, Conflict, NotFound
from ..jobs.engine import get_engine, job_view, latest_job
from ..models import Cluster, Run
from ..pipelines import cluster as pc
from ..services import components as comp
from ..services import hyperpod as hp
from ..services import nodegroups as ngs
from ..services import project as proj

router = APIRouter(prefix="/api/clusters", tags=["clusters"])
plans = APIRouter(prefix="/api/training-plans", tags=["training-plans"])


def _view(c: Cluster) -> dict[str, Any]:
    j = latest_job(c.id)
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
        "network": c.network or {},
        "components": c.components or {},
        "params": {k: v for k, v in (c.params or {}).items() if k != "roles"},
        "idle_policy": c.idle_policy or {},
        "created_at": c.created_at,
        "job": job_view(j) if j else None,
    }


@router.get("")
def list_clusters():
    with session_scope() as s:
        rows = s.query(Cluster).filter(Cluster.status != "deleted").order_by(Cluster.created_at)
        return [_view(c) for c in rows]


@router.get("/azs")
def availability_zones(region: str | None = None):
    return hp.az_ids(region or get_settings().default_region)


@router.get("/discoverable")
def discoverable(region: str | None = None):
    region = region or get_settings().default_region
    sm = aws.client("sagemaker", region)
    with session_scope() as s:
        known = {c.hyperpod_name for c in s.query(Cluster).filter(Cluster.status != "deleted")}
    out = []
    for c in sm.list_clusters(MaxResults=100).get("ClusterSummaries", []):
        if c["ClusterName"] in known:
            continue
        out.append({"name": c["ClusterName"], "arn": c["ClusterArn"], "status": c["ClusterStatus"]})
    return out


class CreateBody(BaseModel):
    name: str
    region: str | None = None
    az_ids: list[str] = Field(min_length=1, max_length=5)
    fsx_capacity_gib: int = 1200
    vpc_cidr: str = "10.192.0.0/16"
    idle_minutes: int = Field(30, ge=5, le=1440)
    budget_usd: float | None = Field(None, ge=0)


@router.post("/preview")
def preview(body: CreateBody):
    region = body.region or get_settings().default_region
    hp.validate_name(body.name)
    params = hp.cfn_parameters(
        name=body.name,
        az_ids=body.az_ids,
        vpc_cidr=body.vpc_cidr,
        fsx_capacity_gib=body.fsx_capacity_gib,
    )
    system_price = instances.on_demand_price(region, hp.SYSTEM_INSTANCE)
    return {
        "stack_name": hp.stack_name(body.name),
        "template_url": hp.template_url(region),
        "parameters": params,
        "system_instance": hp.SYSTEM_INSTANCE,
        "system_price_per_hour": system_price,
        # FSx PERSISTENT_2 250MB/s/TiB ≈ $0.21/GB-month (us-east-1 public price)
        "fsx_monthly_usd_estimate": round(body.fsx_capacity_gib * 0.21, 0),
        "notes": [
            "EKS control plane ~$0.10/h",
            "NAT gateway ~$0.045/h + data",
            "GPU instance groups are added later and billed only while count > 0",
        ],
    }


@router.post("/validate-template")
def validate_template(region: str | None = None):
    region = region or get_settings().default_region
    r = aws.client("cloudformation", region).validate_template(TemplateURL=hp.template_url(region))
    return {"parameters": len(r.get("Parameters", [])), "capabilities": r.get("Capabilities", [])}


def _start(cid: str, job_type: str, payload: dict | None = None) -> str:
    return get_engine().start(job_type, cid, payload or {})


@router.post("")
def create(body: CreateBody):
    region = body.region or get_settings().default_region
    hp.validate_name(body.name)
    proj.require_region(region)
    with session_scope() as s:
        if s.query(Cluster).filter(Cluster.name == body.name, Cluster.status != "deleted").first():
            raise Conflict("cluster.name_taken", f"cluster {body.name} already exists")
        cid = new_id("cl")
        s.add(
            Cluster(
                id=cid,
                name=body.name,
                region=region,
                source="create",
                status="queued",
                params={
                    "az_ids": body.az_ids,
                    "fsx_capacity_gib": body.fsx_capacity_gib,
                    "vpc_cidr": body.vpc_cidr,
                },
                idle_policy={"idle_minutes": body.idle_minutes, "budget_usd": body.budget_usd},
                network={},
                components={},
            )
        )
    return {"id": cid, "job_id": _start(cid, "cluster.create")}


class ImportBody(BaseModel):
    region: str | None = None
    hyperpod_name: str
    idle_minutes: int = Field(30, ge=5, le=1440)
    budget_usd: float | None = Field(None, ge=0)


@router.post("/import")
def import_cluster(body: ImportBody):
    region = body.region or get_settings().default_region
    proj.require_region(region)
    hp.describe_cluster(region, body.hyperpod_name)  # 404 early
    with session_scope() as s:
        cid = new_id("cl")
        s.add(
            Cluster(
                id=cid,
                name=body.hyperpod_name.lower()[:31],
                region=region,
                source="import",
                status="queued",
                hyperpod_name=body.hyperpod_name,
                network={},
                components={},
                params={},
                idle_policy={"idle_minutes": body.idle_minutes, "budget_usd": body.budget_usd},
            )
        )
    return {"id": cid, "job_id": _start(cid, "cluster.import")}


@router.get("/{cid}")
def get_cluster(cid: str, live: bool = True):
    c = pc.load(cid)
    with session_scope() as s:
        view = _view(s.get(Cluster, cid))
    if live and c["hyperpod_name"]:
        try:
            d = hp.describe_cluster(c["region"], c["hyperpod_name"])
            view["live"] = {
                "status": d["ClusterStatus"],
                "failure": d.get("FailureMessage"),
                "node_provisioning_mode": d.get("NodeProvisioningMode"),
                "instance_groups": [
                    {
                        **hp.group_view(g),
                        "price_per_hour": 0
                        if g.get("TrainingPlanArn")
                        else _price(c["region"], g.get("InstanceType")),
                    }
                    for g in d.get("InstanceGroups", [])
                ],
            }
        except AppError as e:
            view["live"] = {"status": "Unavailable", "failure": e.message, "instance_groups": []}
        view["live"]["node_groups"] = []
        if c["eks_name"]:
            try:
                view["live"]["node_groups"] = [
                    ngs.view(c["region"], n) for n in ngs.list_tuningpad(c["region"], c["eks_name"])
                ]
            except Exception as e:  # advisory: EKS API hiccup must not hide the cluster
                view["live"]["node_groups_error"] = f"{type(e).__name__}: {e}"[:300]
    return view


def _price(region: str, itype: str | None) -> float | None:
    if not itype:
        return None
    try:
        return instances.on_demand_price(region, itype)
    except Exception:
        return None


@router.get("/{cid}/nodes")
def nodes(cid: str):
    c = pc.load(cid)
    return hp.list_nodes(c["region"], c["hyperpod_name"])


class ReplaceBody(BaseModel):
    confirm: bool = False


@router.post("/{cid}/nodes/{node_id}/replace")
def replace_node(cid: str, node_id: str, body: ReplaceBody):
    """Replace one HyperPod node with new hardware (EC2 node groups: scale instead)."""
    c = pc.load(cid)
    node = next(
        (n for n in hp.list_nodes(c["region"], c["hyperpod_name"]) if n["id"] == node_id), None
    )
    if not node:
        raise NotFound("node.not_found", f"node {node_id} not found")
    if node["group"] == "system":
        # the only CPU node runs the guardian, KubeRay, LBC: replacing it is an outage
        raise AppError("node.system_group", "the system node cannot be replaced from TuningPad")
    if not body.confirm:
        raise AppError(
            "node.confirm_replace",
            "replacing a node terminates it and loses its instance volumes; confirm to proceed",
            detail={"node": node_id, "group": node["group"]},
        )
    hp.replace_node(c["region"], c["hyperpod_name"], node_id)
    return {"ok": True}


@router.post("/{cid}/components")
def repair(cid: str):
    pc.load(cid)
    return {"job_id": _start(cid, "cluster.components")}


class GroupBody(BaseModel):
    group: str = Field(pattern=r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}$")
    instance_type: str | None = None
    count: int = Field(ge=0, le=20)
    capacity: str = Field("on_demand", pattern="^(on_demand|training_plan|spot)$")
    training_plan_arn: str | None = None
    deep_health_checks: bool = False
    confirm_cost: bool = False


@router.post("/{cid}/groups")
def scale_group(cid: str, body: GroupBody):
    c = pc.load(cid)
    if body.group == hp.SYSTEM_GROUP:
        raise AppError("cluster.system_group", "the system group is managed by the cluster stack")
    payload = body.model_dump()
    if body.capacity != "training_plan":
        payload["training_plan_arn"] = None
    if body.count > 0:
        existing = hp.raw_group(hp.describe_cluster(c["region"], c["hyperpod_name"]), body.group)
        itype = body.instance_type or (existing or {}).get("InstanceType") or ""
        hp.check_existing_capacity(existing, body.capacity, payload["training_plan_arn"])
        if body.capacity == "training_plan":
            if not body.training_plan_arn:
                raise AppError("plan.required", "pick or enter a training plan")
            hp.check_plan(
                c["region"],
                body.training_plan_arn,
                itype,
                body.count,
                (c.get("network") or {}).get("az_ids") or [],
            )
        if not body.confirm_cost:
            prepaid = body.capacity == "training_plan"
            raise AppError(
                "cluster.confirm_cost",
                "scaling up GPU capacity starts billing; confirm the cost to proceed",
                detail={
                    "price_per_hour": 0 if prepaid else _price(c["region"], itype),
                    "prepaid": prepaid,
                },
            )
    return {"job_id": _start(cid, "cluster.scale", payload)}


class NodegroupBody(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    instance_type: str | None = None  # required when creating
    capacity: str = Field("on_demand", pattern="^(on_demand|spot)$")
    count: int = Field(ge=0, le=20)
    max_size: int | None = Field(None, ge=1, le=20)
    efa: bool = False
    az_id: str | None = None
    confirm_cost: bool = False


@router.post("/{cid}/nodegroups")
def apply_nodegroup(cid: str, body: NodegroupBody):
    """Create an EC2 (EKS managed) node group, or set the node count of an existing one."""
    c = pc.load(cid)
    if c["status"] != "ready" or not c["eks_name"]:
        raise AppError("cluster.not_ready", "cluster is not ready")
    existing = ngs.describe(c["region"], c["eks_name"], body.name)
    if existing is None:
        if not body.instance_type:
            raise AppError("cluster.instance_type_required", "instance type is required")
        spec = instances.spec(body.instance_type, serving=True)  # G-family: inference only
        if body.efa and not spec.multi_node:
            raise AppError("nodegroup.no_efa", f"{spec.type} has no multi-node EFA support")
        if hp.raw_group(hp.describe_cluster(c["region"], c["hyperpod_name"]), body.name):
            raise AppError(
                "pool.name_taken",
                f"{body.name} is already a HyperPod instance group",
                detail={"name": body.name, "provider": "hyperpod"},
            )
        itype, capacity = spec.type, body.capacity
    else:
        itype = (existing.get("instanceTypes") or [""])[0]
        capacity = "spot" if existing.get("capacityType") == "SPOT" else "on_demand"
    if body.count > 0 and not body.confirm_cost:
        raise AppError(
            "cluster.confirm_cost",
            "scaling up GPU capacity starts billing; confirm the cost to proceed",
            detail={"price_per_hour": ngs.price(c["region"], itype, capacity), "prepaid": False},
        )
    payload = {**body.model_dump(), "instance_type": itype, "capacity": capacity}
    return {"job_id": _start(cid, "cluster.nodegroup", payload)}


ACTIVE_RUN = {"queued", "preparing", "waiting_capacity", "running", "retrying"}


@router.delete("/{cid}/nodegroups/{name}")
def delete_nodegroup(cid: str, name: str):
    pc.load(cid)
    with session_scope() as s:
        busy = s.query(Run).filter(Run.cluster_id == cid, Run.status.in_(list(ACTIVE_RUN))).all()
    if any((r.compute or {}).get("instance_group") == name for r in busy):
        raise Conflict("nodegroup.in_use", f"an active training run uses {name}")
    return {"job_id": _start(cid, "cluster.delete_nodegroup", {"name": name})}


@router.delete("/{cid}/groups/{group}")
def delete_group(cid: str, group: str):
    pc.load(cid)
    return {"job_id": _start(cid, "cluster.delete_group", {"group": group})}


class PolicyBody(BaseModel):
    idle_minutes: int = Field(30, ge=5, le=1440)
    budget_usd: float | None = Field(None, ge=0)
    groups: dict[str, dict[str, Any]] = Field(default_factory=dict)


@router.put("/{cid}/policy")
def set_policy(cid: str, body: PolicyBody):
    pc.save(cid, idle_policy=body.model_dump())
    try:
        pc.refresh_guardian(cid)
        applied = True
    except Exception:
        applied = False
    return {"ok": True, "applied_to_cluster": applied}


@router.get("/{cid}/ledger")
def ledger(cid: str):
    c = pc.load(cid)
    return comp.read_ledger(c["region"], cid) or {"groups": {}, "total_cost_usd": 0, "actions": []}


@router.delete("/{cid}")
def delete_cluster(cid: str, confirm: str = ""):
    c = pc.load(cid)
    if confirm != c["name"]:
        raise AppError("cluster.confirm_name", "type the cluster name to confirm deletion")
    pc.save(cid, status="deleting")
    return {"job_id": _start(cid, "cluster.delete")}


# ---------------- training plans ----------------


@plans.get("")
def list_training_plans(region: str | None = None):
    return hp.list_plans(region or get_settings().default_region)


@plans.get("/check")
def check_training_plan(arn: str, cluster_id: str, instance_type: str, count: int = 1):
    """Validate a plan (picked or pasted ARN) against a cluster and instance group size."""
    c = pc.load(cluster_id)
    return hp.check_plan(
        c["region"], arn.strip(), instance_type, count, (c.get("network") or {}).get("az_ids") or []
    )


class PlanSearch(BaseModel):
    region: str | None = None
    instance_type: str
    count: int = Field(1, ge=1, le=64)
    duration_hours: int = Field(24, ge=24, le=4368)
    start_after: str | None = None


@plans.post("/search")
def search_training_plans(body: PlanSearch):
    instances.spec(body.instance_type)
    return hp.search_plans(
        body.region or get_settings().default_region,
        body.instance_type,
        body.count,
        body.duration_hours,
        body.start_after,
    )


class PlanBuy(BaseModel):
    region: str | None = None
    offering_id: str
    name: str = Field(pattern=r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,63}$")
    confirm_upfront_fee: float


@plans.post("")
def buy_training_plan(body: PlanBuy):
    """Purchases reserved capacity — irreversible and billed upfront."""
    region = body.region or get_settings().default_region
    sm = aws.client("sagemaker", region)
    resp = sm.create_training_plan(
        TrainingPlanName=body.name, TrainingPlanOfferingId=body.offering_id, Tags=aws.tags()
    )
    return {"arn": resp["TrainingPlanArn"], "confirmed_fee": body.confirm_upfront_fee}
