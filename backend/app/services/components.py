"""Platform components TuningPad installs into a HyperPod EKS cluster.

Every function is idempotent and returns a short detail string for the UI.
Order (see pipelines/cluster.py): access → reach → namespace → irsa → node ECR
pull → security groups → KubeRay → AWS LBC → FSx PVC → guardian.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from ..core import aws, k8s
from ..core.config import REPO_ROOT
from ..core.errors import AppError
from . import kube
from . import project as proj

ASSETS = REPO_ROOT / "cluster_assets"
KUBERAY_CHART_VERSION = "1.7.1"
LBC_CHART_VERSION = "3.5.0"
NVIDIA_PLUGIN_CHART_VERSION = "0.20.1"
EFA_PLUGIN_CHART_VERSION = "0.5.33"
POOL_LABEL = "tuningpad.io/pool"  # EC2 node groups (services/nodegroups.py)
GUARDIAN_IMAGE = "public.ecr.aws/docker/library/python:3.12-slim"
WORKLOAD_SA = "tuningpad-workload"
GUARDIAN_SA = "tuningpad-guardian"
LBC_SA = "aws-load-balancer-controller"
FSX_PVC = "fsx"
GW_PORT = 18765
VLLM_PORT = 8000


def principal_role_arn(region: str) -> str:
    arn = aws.client("sts", region).get_caller_identity()["Arn"]
    role = proj._role_arn_from_assumed(arn)
    if not role:
        raise AppError("cluster.principal", f"cannot derive an IAM principal from {arn}")
    return role


# ---------- EKS access ----------


def ensure_access_entry(region: str, eks_name: str) -> str:
    eks = aws.client("eks", region)
    principal = principal_role_arn(region)
    try:
        eks.create_access_entry(
            clusterName=eks_name, principalArn=principal, type="STANDARD", tags=aws.tag_map()
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
    try:
        eks.associate_access_policy(
            clusterName=eks_name,
            principalArn=principal,
            policyArn="arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
            accessScope={"type": "cluster"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] not in {"ResourceInUseException"}:
            raise
    return f"cluster-admin access for {principal}"


def check_reach(region: str, eks_name: str) -> str:
    from kubernetes import client as kc

    ver = kc.VersionApi(k8s.api_client(region, eks_name)).get_code()
    nodes = k8s.core(region, eks_name).list_node().items
    return f"Kubernetes {ver.git_version}, {len(nodes)} nodes"


def ensure_namespace(region: str, eks_name: str) -> str:
    kube.apply(
        region,
        eks_name,
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": kube.NAMESPACE}},
    )
    return f"namespace {kube.NAMESPACE}"


# ---------- IRSA ----------


def ensure_oidc_provider(region: str, eks_name: str) -> str:
    issuer = aws.client("eks", region).describe_cluster(name=eks_name)["cluster"]["identity"][
        "oidc"
    ]["issuer"]
    host = issuer.removeprefix("https://")
    iam = aws.client("iam", region)
    account = aws.client("sts", region).get_caller_identity()["Account"]
    arn = f"arn:aws:iam::{account}:oidc-provider/{host}"
    try:
        iam.get_open_id_connect_provider(OpenIDConnectProviderArn=arn)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        # IAM no longer validates thumbprints for EKS issuers; the field is still required.
        iam.create_open_id_connect_provider(
            Url=issuer,
            ClientIDList=["sts.amazonaws.com"],
            ThumbprintList=["9e99a48a9960b14926bb7f3b02e22da2b0ab7280"],
            Tags=aws.tags(),
        )
    return arn


def irsa_trust(provider_arn: str, namespace: str, sa: str) -> dict[str, Any]:
    host = provider_arn.split("oidc-provider/")[1]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": provider_arn},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{host}:sub": f"system:serviceaccount:{namespace}:{sa}",
                        f"{host}:aud": "sts.amazonaws.com",
                    }
                },
            }
        ],
    }


def workload_policy(region: str, account: str, bucket: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}"},
            # rollout fan-out from the trainer's agent loop
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:StopRuntimeSession",
                    "bedrock-agentcore:GetAgentRuntime",
                ],
                "Resource": f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/*",
            },
        ],
    }


def guardian_policy(
    bucket: str, hyperpod_arn: str, exec_role_arns: list[str], eks_name: str | None = None
) -> dict[str, Any]:
    stmts: list[dict[str, Any]] = [
        {
            "Effect": "Allow",
            "Action": [
                "sagemaker:DescribeCluster",
                "sagemaker:UpdateCluster",
                "sagemaker:ListClusterNodes",
            ],
            "Resource": hyperpod_arn,
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": f"arn:aws:s3:::{bucket}/clusters/*",
        },
        # S3 needs ListBucket to answer NoSuchKey (first run, no ledger yet) instead of 403
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"},
    ]
    if eks_name:  # EC2 node groups of this one EKS cluster
        _, _, _, region, account = hyperpod_arn.split(":")[:5]
        stmts += [
            {
                "Effect": "Allow",
                "Action": ["eks:DescribeNodegroup", "eks:UpdateNodegroupConfig"],
                "Resource": f"arn:aws:eks:{region}:{account}:nodegroup/{eks_name}/*/*",
            },
            # read-only, no resource-level support
            {
                "Effect": "Allow",
                "Action": "autoscaling:DescribeAutoScalingGroups",
                "Resource": "*",
            },
        ]
    if exec_role_arns:
        stmts.append(
            {
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": exec_role_arns,
                "Condition": {"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
            }
        )
    return {"Version": "2012-10-17", "Statement": stmts}


def ensure_irsa_roles(
    region: str, cluster_name: str, eks_name: str, hyperpod_arn: str, exec_role_arns: list[str]
) -> dict[str, str]:
    res = proj.require_region(region)
    account = aws.client("sts", region).get_caller_identity()["Account"]
    provider = ensure_oidc_provider(region, eks_name)
    bucket = res["bucket"]
    roles = {}
    specs = [
        (
            "workload",
            f"TuningPad-{cluster_name}-workload"[:64],
            kube.NAMESPACE,
            WORKLOAD_SA,
            {"TuningPadWorkload": workload_policy(region, account, bucket)},
        ),
        (
            "guardian",
            f"TuningPad-{cluster_name}-guardian"[:64],
            kube.NAMESPACE,
            GUARDIAN_SA,
            {"TuningPadGuardian": guardian_policy(bucket, hyperpod_arn, exec_role_arns, eks_name)},
        ),
        (
            "lbc",
            f"TuningPad-{cluster_name}-lbc"[:64],
            "kube-system",
            LBC_SA,
            {"AWSLoadBalancerController": json.loads((ASSETS / "lbc_iam_policy.json").read_text())},
        ),
    ]
    for key, role_name, ns, sa, policies in specs:
        roles[key] = proj.ensure_role(region, role_name, irsa_trust(provider, ns, sa), policies)
    for key, ns, sa in (
        ("workload", kube.NAMESPACE, WORKLOAD_SA),
        ("guardian", kube.NAMESPACE, GUARDIAN_SA),
    ):
        kube.apply(
            region,
            eks_name,
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": sa,
                    "namespace": ns,
                    "annotations": {"eks.amazonaws.com/role-arn": roles[key]},
                },
            },
        )
    return roles


def ensure_node_ecr_pull(region: str, exec_role_arns: list[str]) -> str:
    """HyperPod nodes pull our trainer/vLLM images; grant the execution role ECR read."""
    account = aws.client("sts", region).get_caller_identity()["Account"]
    iam = aws.client("iam", region)
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": f"arn:aws:ecr:{region}:{account}:repository/tuningpad-*",
            },
        ],
    }
    for arn in exec_role_arns:
        iam.put_role_policy(
            RoleName=arn.split("/")[-1],
            PolicyName="TuningPadEcrPull",
            PolicyDocument=json.dumps(doc),
        )
    return f"ECR pull on {len(exec_role_arns)} execution role(s)"


# ---------- security groups ----------


def _ensure_sg(ec2, vpc_id: str, name: str, desc: str) -> str:
    found = ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "group-name", "Values": [name]}]
    )["SecurityGroups"]
    if found:
        return found[0]["GroupId"]
    return ec2.create_security_group(
        GroupName=name,
        Description=desc,
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": aws.tags({"Name": name})}],
    )["GroupId"]


def _allow(ec2, sg: str, port: int, source_sg: str, desc: str) -> None:
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "UserIdGroupPairs": [{"GroupId": source_sg, "Description": desc}],
                }
            ],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise


def ensure_security_groups(
    region: str, cluster_name: str, network: dict[str, Any]
) -> dict[str, str]:
    """sg-acr (ACR runtime ENIs) and sg-nlb (vLLM NLBs); cluster SG gets ingress rules.

    HyperPod's VpcConfig SGs cannot change after creation, but their rules can —
    so we only add ingress from our SGs to the existing cluster SG(s).
    """
    ec2 = aws.client("ec2", region)
    vpc = network["vpc_id"]
    acr = _ensure_sg(ec2, vpc, f"tp-{cluster_name}-acr", "TuningPad AgentCore Runtime ENIs")
    nlb = _ensure_sg(ec2, vpc, f"tp-{cluster_name}-nlb", "TuningPad inference NLBs")
    _allow(ec2, nlb, VLLM_PORT, acr, "agents to vLLM NLB")
    for csg in network.get("cluster_sgs", []):
        _allow(ec2, csg, GW_PORT, acr, "agents to rollout gateway")
        _allow(ec2, csg, VLLM_PORT, acr, "agents to vLLM pods")
        _allow(ec2, csg, VLLM_PORT, nlb, "NLB to vLLM pods")
    return {"sg_acr": acr, "sg_nlb": nlb}


# ---------- helm components ----------


def install_kuberay(region: str, eks_name: str) -> str:
    kube.helm(
        region,
        eks_name,
        ["repo", "add", "kuberay", "https://ray-project.github.io/kuberay-helm/", "--force-update"],
    )
    kube.helm(
        region,
        eks_name,
        [
            "upgrade",
            "--install",
            "kuberay-operator",
            "kuberay/kuberay-operator",
            "--version",
            KUBERAY_CHART_VERSION,
            "--namespace",
            "kuberay",
            "--create-namespace",
            "--wait",
            "--timeout",
            "10m",
        ],
    )
    return f"KubeRay operator {KUBERAY_CHART_VERSION}"


POOL_AFFINITY = {
    "nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{"key": POOL_LABEL, "operator": "Exists"}]}]
        }
    }
}


def _helm_values(region: str, eks_name: str, args: list[str], values: dict[str, Any]) -> str:
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(values, f)  # JSON is valid YAML
        return kube.helm(region, eks_name, [*args, "-f", path])
    finally:
        os.unlink(path)


def install_gpu_plugins(region: str, eks_name: str) -> str:
    """NVIDIA + EFA device plugins for EC2 node groups only.

    HyperPod ships its own plugins pinned to ml.* instance types; ours are pinned to nodes
    labelled tuningpad.io/pool, so the two never overlap.
    """
    common = {"affinity": POOL_AFFINITY, "tolerations": [{"operator": "Exists"}]}
    kube.helm(
        region,
        eks_name,
        ["repo", "add", "nvdp", "https://nvidia.github.io/k8s-device-plugin", "--force-update"],
    )
    _helm_values(
        region,
        eks_name,
        [
            "upgrade",
            "--install",
            "tuningpad-nvidia-device-plugin",
            "nvdp/nvidia-device-plugin",
            "--version",
            NVIDIA_PLUGIN_CHART_VERSION,
            "--namespace",
            "kube-system",
        ],
        {**common, "gfd": {"enabled": False}, "nfd": {"enabled": False}},
    )
    kube.helm(
        region,
        eks_name,
        ["repo", "add", "eks", "https://aws.github.io/eks-charts", "--force-update"],
    )
    _helm_values(
        region,
        eks_name,
        [
            "upgrade",
            "--install",
            "tuningpad-efa-device-plugin",
            "eks/aws-efa-k8s-device-plugin",
            "--version",
            EFA_PLUGIN_CHART_VERSION,
            "--namespace",
            "kube-system",
        ],
        common,
    )
    return (
        f"NVIDIA device plugin {NVIDIA_PLUGIN_CHART_VERSION}, "
        f"EFA device plugin {EFA_PLUGIN_CHART_VERSION} (EC2 node groups)"
    )


def install_lbc(region: str, eks_name: str, vpc_id: str, role_arn: str) -> str:
    existing = kube.get(
        region,
        eks_name,
        "apps/v1",
        "Deployment",
        "aws-load-balancer-controller",
        namespace="kube-system",
    )
    if (
        existing
        and not (existing.get("metadata", {}).get("labels") or {}).get(
            "app.kubernetes.io/managed-by"
        )
        == "Helm"
    ):
        return "AWS Load Balancer Controller already present (not managed by TuningPad)"
    kube.helm(
        region,
        eks_name,
        ["repo", "add", "eks", "https://aws.github.io/eks-charts", "--force-update"],
    )
    kube.helm(
        region,
        eks_name,
        [
            "upgrade",
            "--install",
            "aws-load-balancer-controller",
            "eks/aws-load-balancer-controller",
            "--version",
            LBC_CHART_VERSION,
            "--namespace",
            "kube-system",
            "--set",
            f"clusterName={eks_name}",
            "--set",
            f"region={region}",
            "--set",
            f"vpcId={vpc_id}",
            "--set",
            "serviceAccount.create=true",
            "--set",
            f"serviceAccount.name={LBC_SA}",
            "--set",
            f"serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn={role_arn}",
            "--set",
            "nodeSelector.sagemaker\\.amazonaws\\.com/instance-group-name=system",
            "--wait",
            "--timeout",
            "10m",
        ],
    )
    return f"AWS Load Balancer Controller {LBC_CHART_VERSION}"


def ensure_fsx_pvc(region: str, eks_name: str, network: dict[str, Any], cluster_name: str) -> str:
    fsx_id = network.get("fsx_id")
    if not fsx_id:
        raise AppError(
            "cluster.no_fsx",
            "no FSx for Lustre file system found in the cluster VPC (needed for "
            "checkpoints and model cache)",
        )
    pv = f"tuningpad-{cluster_name}-fsx"
    cap = f"{network.get('fsx_capacity_gib') or 1200}Gi"
    kube.apply(
        region,
        eks_name,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {"name": pv},
            "spec": {
                "capacity": {"storage": cap},
                "volumeMode": "Filesystem",
                "accessModes": ["ReadWriteMany"],
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": "",
                "claimRef": {"namespace": kube.NAMESPACE, "name": FSX_PVC},
                "csi": {
                    "driver": "fsx.csi.aws.com",
                    "volumeHandle": fsx_id,
                    "volumeAttributes": {
                        "dnsname": network["fsx_dns"],
                        "mountname": network["fsx_mount"],
                    },
                },
            },
        },
    )
    kube.apply(
        region,
        eks_name,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": FSX_PVC, "namespace": kube.NAMESPACE},
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": "",
                "volumeName": pv,
                "resources": {"requests": {"storage": cap}},
            },
        },
    )
    return f"PVC {kube.NAMESPACE}/{FSX_PVC} → {fsx_id}"


# ---------- guardian ----------


def guardian_config(
    region: str,
    hyperpod_name: str,
    cluster_id: str,
    bucket: str,
    groups: dict[str, dict[str, Any]],
    budget_usd: float | None,
) -> dict[str, Any]:
    return {
        "region": region,
        "cluster": hyperpod_name,
        "bucket": bucket,
        "ledger_key": f"clusters/{cluster_id}/ledger.json",
        "budget_usd": budget_usd or 0,
        "groups": groups,
    }


def install_guardian(region: str, eks_name: str, config: dict[str, Any]) -> str:
    script = Path(ASSETS / "guardian.py").read_text()
    kube.apply(
        region,
        eks_name,
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "tuningpad-guardian", "namespace": kube.NAMESPACE},
            "data": {"guardian.py": script, "guardian.json": json.dumps(config, indent=2)},
        },
    )
    kube.apply(
        region,
        eks_name,
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "tuningpad-guardian"},
            "rules": [
                {"apiGroups": [""], "resources": ["nodes", "pods"], "verbs": ["get", "list"]}
            ],
        },
    )
    kube.apply(
        region,
        eks_name,
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "tuningpad-guardian"},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "tuningpad-guardian",
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": GUARDIAN_SA, "namespace": kube.NAMESPACE}
            ],
        },
    )
    kube.apply(region, eks_name, guardian_cronjob())
    return "guardian CronJob every 5 min"


def guardian_cronjob() -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": "tuningpad-guardian", "namespace": kube.NAMESPACE},
        "spec": {
            "schedule": "*/5 * * * *",
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 2,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": 240,
                    "template": {
                        "spec": {
                            "serviceAccountName": GUARDIAN_SA,
                            "restartPolicy": "Never",
                            "nodeSelector": {
                                "sagemaker.amazonaws.com/instance-group-name": "system"
                            },
                            "containers": [
                                {
                                    "name": "guardian",
                                    "image": GUARDIAN_IMAGE,
                                    "command": [
                                        "sh",
                                        "-c",
                                        "pip install --quiet --no-cache-dir boto3==1.43.83 && "
                                        "python /config/guardian.py",
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "256Mi"},
                                        "limits": {"memory": "512Mi"},
                                    },
                                    "volumeMounts": [{"name": "config", "mountPath": "/config"}],
                                }
                            ],
                            "volumes": [
                                {"name": "config", "configMap": {"name": "tuningpad-guardian"}}
                            ],
                        }
                    },
                }
            },
        },
    }


def read_ledger(region: str, cluster_id: str) -> dict[str, Any] | None:
    bucket = proj.region_resources(region).get("bucket")
    if not bucket:
        return None
    try:
        body = (
            aws.client("s3", region)
            .get_object(Bucket=bucket, Key=f"clusters/{cluster_id}/ledger.json")["Body"]
            .read()
        )
        return json.loads(body)
    except ClientError as e:
        if e.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            return None
        raise
