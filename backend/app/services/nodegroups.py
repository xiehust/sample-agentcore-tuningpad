"""EKS managed node groups (plain EC2) as a second GPU compute pool next to HyperPod.

A TuningPad node group is an EKS managed node group on the HyperPod cluster's own EKS
cluster: AMI `AL2023_x86_64_NVIDIA`, a launch template that joins the HyperPod cluster
security groups (so FSx, the rollout gateway rules and pod-to-pod traffic work unchanged),
an optional EFA layout (all network cards, placement group, single AZ) for multi-node
training, and the node label `tuningpad.io/pool=<name>` that workloads select on.

EC2 capacity is On-Demand or Spot and is governed by the EC2 quotas ("Running On-Demand
P instances", "All P Spot Instance Requests"), independent of the HyperPod quotas.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from botocore.exceptions import ClientError

from ..catalog import instances
from ..core import aws
from ..core.errors import AppError, Conflict
from . import project as proj

POOL_LABEL = "tuningpad.io/pool"
AMI_TYPE = "AL2023_x86_64_NVIDIA"
ROOT_GIB = 500
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
NODE_POLICIES = [
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
]
TAG_POOL = "TuningPadPool"
TAG_EFA = "TuningPadEfa"
TAG_CLUSTER = "TuningPadCluster"

# AL2023 EKS AMIs do not ship the Lustre client that the FSx CSI node plugin mounts with.
USER_DATA = """MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="==TUNINGPAD=="

--==TUNINGPAD==
Content-Type: text/x-shellscript; charset="us-ascii"

#!/bin/bash
set -ux
rpm -q lustre-client >/dev/null 2>&1 || dnf install -y lustre-client || true

--==TUNINGPAD==--
"""


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise AppError(
            "nodegroup.bad_name", "use 2-41 lowercase letters, digits and hyphens", detail={}
        )


def node_role_name(cluster_name: str) -> str:
    return f"TuningPad-{cluster_name}-node"[:64]


def ensure_node_role(region: str, cluster_name: str) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    return proj.ensure_role(region, node_role_name(cluster_name), trust, {}, NODE_POLICIES)


def delete_node_role(region: str, cluster_name: str) -> None:
    iam = aws.client("iam", region)
    name = node_role_name(cluster_name)
    try:
        for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise


def launch_template_data(
    instance_type: str, security_groups: list[str], efa: bool, placement_group: str | None
) -> dict[str, Any]:
    spec = instances.spec(instance_type, serving=True)
    data: dict[str, Any] = {
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {"VolumeSize": ROOT_GIB, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ],
        "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
        "UserData": base64.b64encode(USER_DATA.encode()).decode(),
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": aws.tags({"Name": f"tuningpad-{spec.type}"})}
        ],
    }
    if efa:
        # card 0: IP interface + EFA-only; every other card: EFA-only (no subnet IPs used).
        # MNG rejects SubnetId in the template: subnets come from CreateNodegroup.
        nics = [
            {"NetworkCardIndex": 0, "DeviceIndex": 0, "InterfaceType": "interface"},
            {"NetworkCardIndex": 0, "DeviceIndex": 1, "InterfaceType": "efa-only"},
        ] + [
            {"NetworkCardIndex": i, "DeviceIndex": 0, "InterfaceType": "efa-only"}
            for i in range(1, spec.efa)
        ]
        for n in nics:
            n["Groups"] = security_groups
            n["DeleteOnTermination"] = True
        data["NetworkInterfaces"] = nics
        if placement_group:
            data["Placement"] = {"GroupName": placement_group}
    else:
        data["SecurityGroupIds"] = security_groups
    return data


def _lt_name(eks_name: str, name: str) -> str:
    return f"tuningpad-{eks_name}-{name}"[:128]


def ensure_launch_template(
    region: str, eks_name: str, name: str, data: dict[str, Any]
) -> dict[str, Any]:
    ec2 = aws.client("ec2", region)
    lt = _lt_name(eks_name, name)
    try:
        found = ec2.describe_launch_templates(LaunchTemplateNames=[lt])["LaunchTemplates"][0]
        return {"id": found["LaunchTemplateId"], "version": str(found["LatestVersionNumber"])}
    except ClientError as e:
        if e.response["Error"]["Code"] not in {
            "InvalidLaunchTemplateName.NotFoundException",
            "InvalidLaunchTemplateId.NotFound",
        }:
            raise
    out = ec2.create_launch_template(
        LaunchTemplateName=lt,
        LaunchTemplateData=data,
        TagSpecifications=[{"ResourceType": "launch-template", "Tags": aws.tags()}],
    )["LaunchTemplate"]
    return {"id": out["LaunchTemplateId"], "version": str(out["LatestVersionNumber"])}


def ensure_placement_group(region: str, eks_name: str, name: str) -> str:
    ec2 = aws.client("ec2", region)
    pg = f"tuningpad-{eks_name}-{name}"[:255]
    try:
        ec2.describe_placement_groups(GroupNames=[pg])
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPlacementGroup.Unknown":
            raise
        ec2.create_placement_group(
            GroupName=pg,
            Strategy="cluster",
            TagSpecifications=[{"ResourceType": "placement-group", "Tags": aws.tags()}],
        )
    return pg


def node_security_groups(region: str, eks_name: str, network: dict[str, Any]) -> list[str]:
    """HyperPod cluster SG(s) (self-referencing, FSx, our gateway/vLLM rules) + EKS cluster SG."""
    eks_sg = (
        aws.client("eks", region)
        .describe_cluster(name=eks_name)["cluster"]["resourcesVpcConfig"]
        .get("clusterSecurityGroupId")
    )
    sgs = list(network.get("cluster_sgs") or [])
    if eks_sg and eks_sg not in sgs:
        sgs.append(eks_sg)
    if not sgs:
        raise AppError("nodegroup.no_security_group", "cluster has no security group to join")
    return sgs


def pick_subnets(region: str, network: dict[str, Any], efa: bool, az_id: str | None) -> list[str]:
    subnets = list(network.get("private_subnets") or [])
    if not subnets:
        raise AppError("nodegroup.no_subnet", "cluster has no private subnet")
    if not (efa or az_id):
        return subnets  # Spot/On-Demand can pick any AZ: more capacity pools
    found = aws.client("ec2", region).describe_subnets(SubnetIds=subnets)["Subnets"]
    if az_id:
        found = [s for s in found if s["AvailabilityZoneId"] == az_id]
        if not found:
            raise AppError(
                "nodegroup.az_not_in_cluster", f"cluster has no subnet in {az_id}", detail={}
            )
    # EFA needs every node in one AZ (one placement group)
    return [sorted(found, key=lambda s: -s.get("AvailableIpAddressCount", 0))[0]["SubnetId"]]


def describe(region: str, eks_name: str, name: str) -> dict[str, Any] | None:
    try:
        return aws.client("eks", region).describe_nodegroup(
            clusterName=eks_name, nodegroupName=name
        )["nodegroup"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def list_tuningpad(region: str, eks_name: str) -> list[dict[str, Any]]:
    eks = aws.client("eks", region)
    names, token = [], None
    while True:
        kw: dict[str, Any] = {"clusterName": eks_name, "maxResults": 100}
        if token:
            kw["nextToken"] = token
        resp = eks.list_nodegroups(**kw)
        names += resp.get("nodegroups", [])
        token = resp.get("nextToken")
        if not token:
            break
    out = []
    for n in names:
        ng = describe(region, eks_name, n)
        if ng and (ng.get("tags") or {}).get(TAG_POOL):
            out.append(ng)
    return out


def create(
    region: str,
    eks_name: str,
    *,
    cluster_id: str,
    cluster_name: str,
    network: dict[str, Any],
    name: str,
    instance_type: str,
    capacity: str,
    count: int,
    max_size: int,
    efa: bool,
    az_id: str | None = None,
) -> dict[str, Any]:
    validate_name(name)
    spec = instances.spec(instance_type, serving=True)
    if capacity not in ("on_demand", "spot"):
        raise AppError("nodegroup.bad_capacity", "EC2 node groups use on_demand or spot")
    if efa and not spec.multi_node:
        raise AppError("nodegroup.no_efa", f"{spec.type} has no multi-node EFA support")
    if describe(region, eks_name, name):
        raise Conflict("nodegroup.exists", f"node group {name} already exists")
    role = ensure_node_role(region, cluster_name)
    sgs = node_security_groups(region, eks_name, network)
    pg = ensure_placement_group(region, eks_name, name) if efa else None
    lt = ensure_launch_template(
        region, eks_name, name, launch_template_data(spec.type, sgs, efa, pg)
    )
    subnets = pick_subnets(region, network, efa, az_id)
    aws.client("eks", region).create_nodegroup(
        clusterName=eks_name,
        nodegroupName=name,
        # always created empty: it turns ACTIVE in ~1 min and capacity shortages then show up
        # as ASG retries on a scale-up, instead of failing the whole node group creation
        scalingConfig={"minSize": 0, "maxSize": max(1, max_size, count), "desiredSize": 0},
        subnets=subnets,
        instanceTypes=[spec.type],
        amiType=AMI_TYPE,
        capacityType="SPOT" if capacity == "spot" else "ON_DEMAND",
        nodeRole=role,
        launchTemplate={"id": lt["id"], "version": lt["version"]},
        labels={POOL_LABEL: name, "tuningpad.io/instance-type": spec.type},
        # keep cluster add-ons off GPU nodes; TuningPad workloads tolerate NoSchedule
        taints=[{"key": "nvidia.com/gpu", "value": "true", "effect": "NO_SCHEDULE"}],
        updateConfig={"maxUnavailable": 1},
        tags=aws.tag_map(
            {TAG_POOL: name, TAG_EFA: "true" if efa else "false", TAG_CLUSTER: cluster_id}
        ),
    )
    return {
        "name": name,
        "subnets": subnets,
        "efa": efa,
        "launch_template": lt["id"],
        "pending_count": count,
    }


def scale(region: str, eks_name: str, name: str, count: int) -> None:
    ng = describe(region, eks_name, name)
    if not ng:
        raise AppError("nodegroup.not_found", f"node group {name} not found")
    sc = ng["scalingConfig"]
    aws.client("eks", region).update_nodegroup_config(
        clusterName=eks_name,
        nodegroupName=name,
        scalingConfig={
            "minSize": 0,
            "maxSize": max(sc.get("maxSize", 1), count, 1),
            "desiredSize": count,
        },
    )


def delete(region: str, eks_name: str, name: str) -> None:
    try:
        aws.client("eks", region).delete_nodegroup(clusterName=eks_name, nodegroupName=name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise


def cleanup_artifacts(region: str, eks_name: str, name: str) -> None:
    """Launch template + placement group, after the node group is gone."""
    ec2 = aws.client("ec2", region)
    for call, kw in (
        (ec2.delete_launch_template, {"LaunchTemplateName": _lt_name(eks_name, name)}),
        (ec2.delete_placement_group, {"GroupName": f"tuningpad-{eks_name}-{name}"[:255]}),
    ):
        try:
            call(**kw)
        except ClientError:
            pass  # not created (non-EFA) or already gone


def asg_names(region: str, ng: dict[str, Any]) -> list[str]:
    """The node group's ASG(s); EKS links them only once creation completes, so fall back
    to the eks:nodegroup-name tag."""
    names = [a["name"] for a in (ng.get("resources") or {}).get("autoScalingGroups") or []]
    if names or not ng.get("clusterName"):
        return names
    try:
        groups = aws.client("autoscaling", region).describe_auto_scaling_groups(
            Filters=[
                {"Name": "tag:eks:nodegroup-name", "Values": [ng["nodegroupName"]]},
                {"Name": "tag:eks:cluster-name", "Values": [ng["clusterName"]]},
            ]
        )["AutoScalingGroups"]
    except ClientError:
        return []
    return [g["AutoScalingGroupName"] for g in groups]


def asg_counts(region: str, ng: dict[str, Any]) -> dict[str, int]:
    names = asg_names(region, ng)
    if not names:
        return {"running": 0, "in_service": 0}
    groups = aws.client("autoscaling", region).describe_auto_scaling_groups(
        AutoScalingGroupNames=names
    )["AutoScalingGroups"]
    inst = [i for g in groups for i in g.get("Instances", [])]
    return {
        "running": sum(1 for i in inst if not i["LifecycleState"].startswith("Terminat")),
        "in_service": sum(1 for i in inst if i["LifecycleState"] == "InService"),
    }


def latest_failure(region: str, ng: dict[str, Any]) -> str | None:
    """Why capacity is missing: newest failed ASG activity, else node group health issues."""
    names = asg_names(region, ng)
    if names:
        try:
            acts = aws.client("autoscaling", region).describe_scaling_activities(
                AutoScalingGroupName=names[0], MaxRecords=5
            )["Activities"]
        except ClientError:
            acts = []
        for a in acts:
            if a.get("StatusCode") == "Successful":
                break
            if a.get("StatusCode") in ("Failed", "Cancelled"):
                return (a.get("StatusMessage") or a.get("Description") or "")[:300]
    issues = (ng.get("health") or {}).get("issues") or []
    if issues:
        return f"{issues[0].get('code')}: {issues[0].get('message')}"[:300]
    return None


def view(region: str, ng: dict[str, Any], *, live: bool = True) -> dict[str, Any]:
    sc = ng.get("scalingConfig") or {}
    tags = ng.get("tags") or {}
    counts = asg_counts(region, ng) if live else {"running": 0, "in_service": 0}
    itype = (ng.get("instanceTypes") or [""])[0]
    capacity = "spot" if ng.get("capacityType") == "SPOT" else "on_demand"
    return {
        "name": ng["nodegroupName"],
        "provider": "ec2",
        "instance_type": itype,
        "capacity": capacity,
        "efa": tags.get(TAG_EFA) == "true",
        "status": ng.get("status"),
        "target": sc.get("desiredSize", 0),
        "max": sc.get("maxSize", 0),
        "current": counts["in_service"],
        "running": counts["running"],
        "subnets": ng.get("subnets") or [],
        "failure": latest_failure(region, ng)
        if live and counts["in_service"] < sc.get("desiredSize", 0)
        else None,
        "price_per_hour": price(region, itype, capacity, live=live),
    }


def price(region: str, instance_type: str, capacity: str, *, live: bool = True) -> float | None:
    if not live or not instance_type:
        return None
    try:
        if capacity == "spot":
            return instances.ec2_spot_price(region, instance_type)
        return instances.ec2_on_demand_price(region, instance_type)
    except Exception:
        return None
