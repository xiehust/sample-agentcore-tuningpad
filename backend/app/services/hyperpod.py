"""SageMaker HyperPod (EKS) helpers: CloudFormation quick-setup parameters,
cluster discovery, instance-group scaling, training plans.

New clusters are created with the official HyperPod EKS main stack
(aws/sagemaker-hyperpod-cluster-setup, hosted per region in
`aws-sagemaker-hyperpod-cluster-setup-<region>-prod`). It builds VPC, private
subnets, the EFA security group, EKS, lifecycle bucket, execution role, the
HyperPod helm chart, FSx for Lustre (+ `fsx-claim` PVC) and the HyperPod
cluster with a CPU `system` instance group. GPU instance groups are added and
scaled afterwards through `UpdateCluster` by TuningPad.
"""

from __future__ import annotations

import json
import re
from typing import Any

from botocore.exceptions import ClientError

from ..core import aws
from ..core.errors import AppError

SYSTEM_GROUP = "system"
SYSTEM_INSTANCE = "ml.m5.2xlarge"
EKS_VERSION = "1.33"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}$")
DEEP_HEALTH_CHECKS = ["InstanceStress", "InstanceConnectivity"]


def template_url(region: str) -> str:
    return (
        f"https://aws-sagemaker-hyperpod-cluster-setup-{region}-prod.s3.{region}.amazonaws.com"
        "/templates/main-stack-eks-based-template.yaml"
    )


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise AppError(
            "cluster.invalid_name",
            "cluster name must be 3-31 chars: lowercase letters, digits and '-', starting "
            "with a letter",
        )


def stack_name(name: str) -> str:
    return f"tuningpad-{name}"


def cfn_parameters(
    *,
    name: str,
    az_ids: list[str],
    vpc_cidr: str = "10.192.0.0/16",
    fsx_capacity_gib: int = 1200,
    system_instance: str = SYSTEM_INSTANCE,
    tags: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Parameters for the official main stack. Features TuningPad does not use are off."""
    if not az_ids:
        raise AppError("cluster.az_required", "at least one availability zone id is required")
    if fsx_capacity_gib != 1200 and (fsx_capacity_gib < 2400 or fsx_capacity_gib % 2400):
        raise AppError("cluster.invalid_fsx", "FSx capacity must be 1200 GiB or a multiple of 2400")
    groups = [
        {
            "InstanceGroupName": SYSTEM_GROUP,
            "InstanceType": system_instance,
            "InstanceCount": 1,
            "ThreadsPerCore": 1,
        }
    ]
    prefix = f"tp-{name}"[:28]
    params = {
        "ResourceNamePrefix": prefix,
        "ResourceNameShortPrefix": f"tp{name.replace('-', '')}"[:8],
        "VpcCIDR": vpc_cidr,
        "AvailabilityZoneIds": ",".join(az_ids),
        "KubernetesVersion": EKS_VERSION,
        "EKSClusterName": f"tp-{name}-eks",
        "HyperPodClusterName": f"tp-{name}",
        "NodeRecovery": "Automatic",
        # Continuous provisioning is required for Spot instance groups and lets a
        # scale-up proceed with partial capacity.
        "NodeProvisioningMode": "Continuous",
        "InstanceGroupSettings1": json.dumps(groups),
        "FsxAvailabilityZoneId": az_ids[0],
        "StorageCapacity": str(fsx_capacity_gib),
        "EnableHPInferenceFeature": "false",
        "EnableObservabilityFeature": "false",
        "EnableHPTrainingOperatorFeature": "false",
        "CreateTaskGovernanceClusterPolicyStack": "false",
        "Tags": json.dumps([{"Key": k, "Value": v} for k, v in (tags or {}).items()]),
    }
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()]


def stack_status(region: str, name: str) -> dict[str, Any] | None:
    try:
        s = aws.client("cloudformation", region).describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as e:
        if "does not exist" in e.response["Error"].get("Message", ""):
            return None
        raise
    return {
        "status": s["StackStatus"],
        "reason": s.get("StackStatusReason"),
        "outputs": {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])},
        "stack_id": s["StackId"],
    }


def stack_failure_reason(region: str, name: str) -> str | None:
    """First CREATE_FAILED event (nested stacks report the real cause there)."""
    cfn = aws.client("cloudformation", region)
    try:
        events = cfn.describe_stack_events(StackName=name)["StackEvents"]
    except ClientError:
        return None
    failed = [e for e in events if e.get("ResourceStatus", "").endswith("FAILED")]
    if not failed:
        return None
    e = failed[-1]
    return f"{e.get('LogicalResourceId')}: {e.get('ResourceStatusReason')}"


def stack_progress(region: str, name: str) -> str:
    cfn = aws.client("cloudformation", region)
    try:
        res = cfn.list_stack_resources(StackName=name).get("StackResourceSummaries", [])
    except ClientError:
        return ""
    nested = [r for r in res if r["ResourceType"] == "AWS::CloudFormation::Stack"]
    done = [r for r in nested if r["ResourceStatus"] == "CREATE_COMPLETE"]
    running = [
        r["LogicalResourceId"] for r in nested if r["ResourceStatus"] == "CREATE_IN_PROGRESS"
    ]
    return f"{len(done)}/{len(nested)} nested stacks" + (
        f" · {', '.join(running[:3])}" if running else ""
    )


def describe_cluster(region: str, hyperpod_name: str) -> dict[str, Any]:
    try:
        return aws.client("sagemaker", region).describe_cluster(ClusterName=hyperpod_name)
    except ClientError as e:
        if e.response["Error"]["Code"] in {"ResourceNotFound", "ValidationException"}:
            raise AppError(
                "cluster.not_found", f"HyperPod cluster {hyperpod_name} not found", status=404
            ) from e
        raise


def discover(region: str, hyperpod_name: str) -> dict[str, Any]:
    """Everything TuningPad needs to operate an existing HyperPod EKS cluster."""
    hp = describe_cluster(region, hyperpod_name)
    eks_arn = (hp.get("Orchestrator") or {}).get("Eks", {}).get("ClusterArn")
    if not eks_arn:
        raise AppError("cluster.not_eks", f"{hyperpod_name} is not EKS-orchestrated")
    eks_name = eks_arn.split("/")[-1]
    eks = aws.client("eks", region).describe_cluster(name=eks_name)["cluster"]
    vpc = hp.get("VpcConfig") or {}
    subnets = vpc.get("Subnets", [])
    ec2 = aws.client("ec2", region)
    sn = ec2.describe_subnets(SubnetIds=subnets)["Subnets"] if subnets else []
    vpc_id = sn[0]["VpcId"] if sn else eks["resourcesVpcConfig"]["vpcId"]
    fsx = find_fsx(region, vpc_id, subnets)
    return {
        "hyperpod_arn": hp["ClusterArn"],
        "status": hp["ClusterStatus"],
        "eks_name": eks_name,
        "eks_version": eks.get("version"),
        "auth_mode": (eks.get("accessConfig") or {}).get("authenticationMode"),
        "network": {
            "vpc_id": vpc_id,
            "private_subnets": subnets,
            "azs": sorted({s["AvailabilityZone"] for s in sn}),
            "az_ids": sorted({s["AvailabilityZoneId"] for s in sn}),
            "cluster_sgs": vpc.get("SecurityGroupIds", []),
            "fsx_id": fsx.get("id") if fsx else None,
            "fsx_dns": fsx.get("dns") if fsx else None,
            "fsx_mount": fsx.get("mount") if fsx else None,
            "fsx_capacity_gib": fsx.get("capacity") if fsx else None,
        },
        "instance_groups": [group_view(g) for g in hp.get("InstanceGroups", [])],
        "node_provisioning_mode": hp.get("NodeProvisioningMode"),
    }


def find_fsx(region: str, vpc_id: str, subnets: list[str]) -> dict[str, Any] | None:
    fsx = aws.client("fsx", region)
    found = []
    for fs in fsx.describe_file_systems().get("FileSystems", []):
        if fs.get("FileSystemType") != "LUSTRE" or fs.get("Lifecycle") != "AVAILABLE":
            continue
        if fs.get("VpcId") != vpc_id:
            continue
        found.append(fs)
    if not found:
        return None
    # prefer one in our subnets
    found.sort(key=lambda f: 0 if set(f.get("SubnetIds", [])) & set(subnets) else 1)
    fs = found[0]
    return {
        "id": fs["FileSystemId"],
        "dns": fs["DNSName"],
        "mount": fs["LustreConfiguration"]["MountName"],
        "capacity": fs["StorageCapacity"],
    }


def group_view(g: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": g.get("InstanceGroupName"),
        "instance_type": g.get("InstanceType"),
        "target": g.get("TargetCount", g.get("InstanceCount")),
        "current": g.get("CurrentCount", 0),
        "status": g.get("Status"),
        "training_plan_arn": g.get("TrainingPlanArn"),
        "spot": bool((g.get("CapacityRequirements") or {}).get("Spot") is not None),
        "deep_health_checks": g.get("OnStartDeepHealthChecks") or [],
    }


def _template_group(hp: dict[str, Any]) -> dict[str, Any]:
    """Copy ExecutionRole + LifeCycleConfig from an existing group (system)."""
    groups = hp.get("InstanceGroups", [])
    if not groups:
        raise AppError("cluster.no_groups", "cluster has no instance group to copy settings from")
    base = next((g for g in groups if g["InstanceGroupName"] == SYSTEM_GROUP), groups[0])
    return {"ExecutionRole": base["ExecutionRole"], "LifeCycleConfig": base["LifeCycleConfig"]}


def group_spec(
    hp: dict[str, Any],
    *,
    name: str,
    instance_type: str,
    count: int,
    capacity: str = "on_demand",
    training_plan_arn: str | None = None,
    deep_health_checks: bool = False,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if existing:  # keep the group's own settings, only change the count
        spec = {
            k: existing[k]
            for k in (
                "InstanceGroupName",
                "InstanceType",
                "ExecutionRole",
                "LifeCycleConfig",
                "ThreadsPerCore",
                "TrainingPlanArn",
                "OnStartDeepHealthChecks",
                "CapacityRequirements",
                "InstanceStorageConfigs",
                "OverrideVpcConfig",
            )
            if existing.get(k) is not None
        }
        spec["InstanceCount"] = count
        return spec
    spec = {
        "InstanceGroupName": name,
        "InstanceType": instance_type if instance_type.startswith("ml.") else f"ml.{instance_type}",
        "InstanceCount": count,
        "ThreadsPerCore": 1,
        **_template_group(hp),
    }
    if deep_health_checks:
        spec["OnStartDeepHealthChecks"] = DEEP_HEALTH_CHECKS
    if capacity == "training_plan":
        if not training_plan_arn:
            raise AppError("cluster.plan_required", "training plan ARN is required")
        spec["TrainingPlanArn"] = training_plan_arn
    elif capacity == "spot":
        if hp.get("NodeProvisioningMode") != "Continuous":
            raise AppError(
                "cluster.spot_needs_continuous",
                "Spot instance groups need a cluster created with continuous provisioning",
            )
        spec["CapacityRequirements"] = {"Spot": {}}
    return spec


def raw_group(hp: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((g for g in hp.get("InstanceGroups", []) if g["InstanceGroupName"] == name), None)


def update_group(region: str, hyperpod_name: str, spec: dict[str, Any]) -> None:
    aws.client("sagemaker", region).update_cluster(ClusterName=hyperpod_name, InstanceGroups=[spec])


def delete_group(region: str, hyperpod_name: str, name: str) -> None:
    aws.client("sagemaker", region).update_cluster(
        ClusterName=hyperpod_name, InstanceGroups=[], InstanceGroupsToDelete=[name]
    )


def list_nodes(region: str, hyperpod_name: str) -> list[dict[str, Any]]:
    sm = aws.client("sagemaker", region)
    out, token = [], None
    while True:
        kwargs: dict[str, Any] = {"ClusterName": hyperpod_name, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        resp = sm.list_cluster_nodes(**kwargs)
        for n in resp.get("ClusterNodeSummaries", []):
            out.append(
                {
                    "id": n.get("InstanceId"),
                    "group": n.get("InstanceGroupName"),
                    "type": n.get("InstanceType"),
                    "status": (n.get("InstanceStatus") or {}).get("Status"),
                    "message": (n.get("InstanceStatus") or {}).get("Message"),
                    "launch_time": n.get("LaunchTime"),
                }
            )
        token = resp.get("NextToken")
        if not token:
            return out


def search_plans(
    region: str, instance_type: str, count: int, duration_hours: int, start_after: str | None = None
) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "InstanceType": instance_type if instance_type.startswith("ml.") else f"ml.{instance_type}",
        "InstanceCount": count,
        "DurationHours": duration_hours,
        "TargetResources": ["hyperpod-cluster"],
    }
    if start_after:
        kwargs["StartTimeAfter"] = start_after
    resp = aws.client("sagemaker", region).search_training_plan_offerings(**kwargs)
    out = []
    for o in resp.get("TrainingPlanOfferings", []):
        rs = (o.get("ReservedCapacityOfferings") or [{}])[0]
        out.append(
            {
                "id": o["TrainingPlanOfferingId"],
                "upfront_fee": float(o.get("UpfrontFee", 0) or 0),
                "currency": o.get("CurrencyCode"),
                "duration_hours": o.get("DurationHours"),
                "start": rs.get("StartTime"),
                "end": rs.get("EndTime"),
                "az": rs.get("AvailabilityZone"),
                "instance_type": rs.get("InstanceType"),
                "count": rs.get("InstanceCount"),
            }
        )
    return out


PLAN_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:sagemaker:[a-z0-9-]+:\d{12}:training-plan/[A-Za-z0-9-]+$"
)
PLAN_USABLE = {"Active", "Scheduled"}


def plan_view(p: dict[str, Any]) -> dict[str, Any]:
    """Normalize a ListTrainingPlans summary / DescribeTrainingPlan response."""
    rcs = p.get("ReservedCapacitySummaries") or []
    rc = rcs[0] if rcs else {}
    return {
        "arn": p["TrainingPlanArn"],
        "name": p.get("TrainingPlanName"),
        "status": p.get("Status"),
        "start": p.get("StartTime"),
        "end": p.get("EndTime"),
        "instances": p.get("TotalInstanceCount"),
        "available": p.get("AvailableInstanceCount"),
        "in_use": p.get("InUseInstanceCount"),
        "upfront_fee": p.get("UpfrontFee"),
        "currency": p.get("CurrencyCode"),
        "targets": p.get("TargetResources") or [],
        "instance_type": (rc.get("InstanceType") or "").removeprefix("ml.") or None,
        "az": rc.get("AvailabilityZone"),
        "az_id": rc.get("AvailabilityZoneId"),
    }


def list_plans(region: str) -> list[dict[str, Any]]:
    sm = aws.client("sagemaker", region)
    out, token = [], None
    while True:
        kwargs: dict[str, Any] = {"MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        resp = sm.list_training_plans(**kwargs)
        out += [plan_view(p) for p in resp.get("TrainingPlanSummaries", [])]
        token = resp.get("NextToken")
        if not token:
            return out


def describe_plan(region: str, arn: str) -> dict[str, Any]:
    if not PLAN_ARN_RE.match(arn or ""):
        raise AppError("plan.bad_arn", f"not a SageMaker training plan ARN: {arn}")
    if arn.split(":")[3] != region:
        raise AppError("plan.region_mismatch", f"training plan is not in {region}")
    try:
        d = aws.client("sagemaker", region).describe_training_plan(
            TrainingPlanName=arn.rsplit("/", 1)[1]
        )
    except ClientError as e:
        raise AppError("plan.not_found", f"training plan not found: {arn}") from e
    return plan_view(d)


def check_plan(
    region: str, arn: str, instance_type: str, count: int, cluster_az_ids: list[str]
) -> dict[str, Any]:
    """Validate a training plan for a HyperPod instance group of `count` × `instance_type`."""
    plan = describe_plan(region, arn)
    if plan["status"] not in PLAN_USABLE:
        raise AppError(
            "plan.not_usable",
            f"training plan is {plan['status']}",
            detail={"status": plan["status"]},
        )
    if "hyperpod-cluster" not in plan["targets"]:
        raise AppError("plan.wrong_target", "training plan is not for HyperPod clusters")
    itype = instance_type.removeprefix("ml.")
    if plan["instance_type"] and plan["instance_type"] != itype:
        raise AppError(
            "plan.instance_mismatch",
            f"training plan reserves {plan['instance_type']}, not {itype}",
            detail={"plan": plan["instance_type"], "requested": itype},
        )
    if count > (plan["instances"] or 0):
        raise AppError(
            "plan.too_small",
            f"training plan has {plan['instances']} instances",
            detail={"instances": plan["instances"], "requested": count},
        )
    if plan["az_id"] and cluster_az_ids and plan["az_id"] not in cluster_az_ids:
        raise AppError(
            "plan.az_not_in_cluster",
            f"training plan capacity is in {plan['az_id']}; the cluster has no subnet there",
            detail={"az": plan["az_id"], "cluster": cluster_az_ids},
        )
    return plan


def group_capacity(g: dict[str, Any]) -> str:
    if g.get("TrainingPlanArn"):
        return "training_plan"
    if (g.get("CapacityRequirements") or {}).get("Spot") is not None:
        return "spot"
    return "on_demand"


def check_existing_capacity(g: dict[str, Any] | None, capacity: str, plan_arn: str | None) -> None:
    """Scaling an existing group keeps its capacity source; refuse a silent mode switch."""
    if not g:
        return
    have = group_capacity(g)
    if have != capacity or (capacity == "training_plan" and g.get("TrainingPlanArn") != plan_arn):
        raise AppError(
            "cluster.group_capacity_mismatch",
            f"group {g['InstanceGroupName']} uses {have}; pick another group name",
            detail={"group": g["InstanceGroupName"], "capacity": have},
        )


def az_ids(region: str) -> list[dict[str, str]]:
    zones = aws.client("ec2", region).describe_availability_zones(
        Filters=[{"Name": "zone-type", "Values": ["availability-zone"]}]
    )["AvailabilityZones"]
    return [{"name": z["ZoneName"], "id": z["ZoneId"]} for z in zones if z["State"] == "available"]


def latest_group_failure(region: str, hyperpod_name: str, group: str) -> str | None:
    """Most recent provisioning failure message for an instance group (cluster events)."""
    sm = aws.client("sagemaker", region)
    try:
        events = sm.list_cluster_events(ClusterName=hyperpod_name, MaxResults=20).get("Events", [])
    except (ClientError, AttributeError):
        return None
    for e in events:
        if e.get("InstanceGroupName") != group:
            continue
        if "Successfully" in (e.get("Description") or "") and "Instance creation" in e.get(
            "Description", ""
        ):
            return None  # newer success than any failure
        if "Failed to provision" in (e.get("Description") or ""):
            try:
                d = sm.describe_cluster_event(ClusterName=hyperpod_name, EventId=e["EventId"])
                meta = d["EventDetails"]["EventDetails"]["EventMetadata"]["Instance"]
                return meta.get("FailureMessage") or e["Description"]
            except (ClientError, KeyError):
                return e["Description"]
    return None
