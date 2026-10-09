"""Project-level (per region) resources: bucket, IAM roles, ECR repositories.

Everything is idempotent ("ensure"): look up first, create only when missing,
re-apply policies on every run so drift is corrected.
"""

from __future__ import annotations

import json
import shutil
from typing import Any

from botocore.exceptions import ClientError

from ..core import aws
from ..core.config import get_settings
from ..core.db import session_scope
from ..models import Project

PROJECT_ID = "default"
AGENT_ECR_REPO = "tuningpad-agents"
TRAINER_ECR_REPO = "tuningpad-trainer"


def names(account: str, region: str) -> dict[str, str]:
    return {
        "bucket": f"tuningpad-{account}-{region}",
        "acr_role": f"TuningPad-AgentRuntime-{region}",
        "codebuild_role": f"TuningPad-CodeBuild-{region}",
        "agent_repo": AGENT_ECR_REPO,
        "trainer_repo": TRAINER_ECR_REPO,
    }


def get_project() -> dict[str, Any]:
    with session_scope() as s:
        p = s.get(Project, PROJECT_ID)
        if not p:
            p = Project(id=PROJECT_ID, default_region=get_settings().default_region, regions={})
            s.add(p)
            s.flush()
        return {
            "account_id": p.account_id,
            "principal_arn": p.principal_arn,
            "default_region": p.default_region,
            "regions": p.regions or {},
            "setup_status": p.setup_status,
            "last_preflight": p.last_preflight or {},
        }


def region_resources(region: str) -> dict[str, Any]:
    """Resources Setup created in `region` ({} when Setup has not run there)."""
    return (get_project()["regions"] or {}).get(region, {})


def require_region(region: str) -> dict[str, Any]:
    from ..core.errors import AppError

    res = region_resources(region)
    if res.get("status") != "ready":
        raise AppError(
            "setup.region_not_ready",
            f"project setup has not completed in {region}; run Settings → Project setup first",
            status=409,
            detail={"region": region},
        )
    return res


def save_region(region: str, data: dict[str, Any]) -> None:
    with session_scope() as s:
        p = s.get(Project, PROJECT_ID)
        if not p:
            p = Project(id=PROJECT_ID, default_region=get_settings().default_region, regions={})
            s.add(p)
        regions = dict(p.regions or {})
        regions[region] = {**regions.get(region, {}), **data}
        p.regions = regions


def save_identity(account: str, arn: str) -> None:
    with session_scope() as s:
        p = s.get(Project, PROJECT_ID)
        if not p:
            p = Project(id=PROJECT_ID, default_region=get_settings().default_region, regions={})
            s.add(p)
        p.account_id = account
        p.principal_arn = arn


# ---------------- preflight ----------------

REQUIRED_ACTIONS = [
    "sagemaker:CreateCluster",
    "sagemaker:UpdateCluster",
    "sagemaker:DeleteCluster",
    "eks:DescribeCluster",
    "eks:CreateAccessEntry",
    "cloudformation:CreateStack",
    "bedrock-agentcore:CreateAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntime",
    "iam:CreateRole",
    "iam:PassRole",
    "ecr:CreateRepository",
    "codebuild:StartBuild",
    "s3:CreateBucket",
    "s3:PutObject",
    "ec2:CreateSecurityGroup",
    "ec2:AuthorizeSecurityGroupIngress",
    # eval traces: Logs Insights over aws/spans, Transaction Search status
    "logs:StartQuery",
    "logs:GetQueryResults",
    "xray:GetTraceSegmentDestination",
]


def _role_arn_from_assumed(arn: str) -> str | None:
    # arn:aws:sts::123:assumed-role/RoleName/session -> arn:aws:iam::123:role/RoleName
    if ":assumed-role/" not in arn:
        return arn if ":user/" in arn or ":role/" in arn else None
    acct = arn.split(":")[4]
    role = arn.split(":assumed-role/")[1].split("/")[0]
    return f"arn:aws:iam::{acct}:role/{role}"


def preflight(region: str) -> dict[str, Any]:
    from ..catalog import instances

    checks: list[dict[str, Any]] = []

    def add(cid: str, status: str, message: str, detail: Any = None):
        checks.append({"id": cid, "status": status, "message": message, "detail": detail})

    ident = aws.client("sts", region).get_caller_identity()
    account, arn = ident["Account"], ident["Arn"]
    save_identity(account, arn)
    add("identity", "ok", f"{arn} ({account})")

    principal = _role_arn_from_assumed(arn)
    if principal:
        try:
            resp = aws.client("iam", region).simulate_principal_policy(
                PolicySourceArn=principal, ActionNames=REQUIRED_ACTIONS
            )
            denied = [
                r["EvalActionName"]
                for r in resp.get("EvaluationResults", [])
                if r.get("EvalDecision") != "allowed"
            ]
            if denied:
                add("permissions", "fail", f"{len(denied)} required actions denied", denied)
            else:
                add("permissions", "ok", f"{len(REQUIRED_ACTIONS)} required actions allowed")
        except ClientError as e:
            add("permissions", "warn", f"cannot simulate policy: {e.response['Error']['Code']}")
    else:
        add("permissions", "warn", "principal type not simulatable; skipped")

    try:
        quotas = instances.cluster_quotas(region)
        usable = {
            t: q["on_demand"]
            for t in instances.CATALOG
            if (q := instances.quota_for(quotas, t))["on_demand"]
        }
        if usable:
            add(
                "gpu_quota",
                "ok",
                "HyperPod GPU quota: " + ", ".join(f"{t}×{int(v)}" for t, v in usable.items()),
                usable,
            )
        else:
            add(
                "gpu_quota",
                "warn",
                f"no P-family 'for cluster usage' quota in {region}; request an increase "
                "or use a training plan",
            )
    except ClientError as e:
        add("gpu_quota", "warn", f"cannot read quotas: {e.response['Error']['Code']}")

    from ..services.tools import tool_status

    for name, st in tool_status().items():
        add(
            f"tool.{name}",
            "ok" if st["path"] else "warn",
            f"{name} {st['version'] or 'not installed (installed automatically when needed)'}",
        )
    docker = shutil.which("docker")
    add(
        "tool.docker",
        "ok" if docker else "warn",
        "docker available" if docker else "docker missing: agent images cannot be built locally",
    )

    toolkit = get_settings().toolkit_path
    ok = (toolkit / "pyproject.toml").is_file()
    add(
        "toolkit",
        "ok" if ok else "fail",
        f"agentcore-rl-toolkit at {toolkit}" + ("" if ok else " not found"),
    )

    result = {
        "region": region,
        "account": account,
        "checks": checks,
        "ok": all(c["status"] != "fail" for c in checks),
    }
    with session_scope() as s:
        p = s.get(Project, PROJECT_ID)
        if p:
            p.last_preflight = result
    return result


# ---------------- ensure helpers ----------------


def ensure_bucket(region: str, bucket: str) -> None:
    s3 = aws.client("s3", region)
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        if e.response["Error"]["Code"] not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        kwargs: dict[str, Any] = {"Bucket": bucket}
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": aws.tags()})
    # rollout JSONs embed the per-session gateway key → expire them; smoke results fast.
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "smoke-1d",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "smoke/"},
                    "Expiration": {"Days": 1},
                },
                {
                    "ID": "rollouts-14d",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "rollouts/"},
                    "Expiration": {"Days": 14},
                },
                {
                    "ID": "build-ctx-7d",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "build/"},
                    "Expiration": {"Days": 7},
                },
                {
                    "ID": "abort-mpu",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3},
                },
            ]
        },
    )


def ensure_role(
    region: str, name: str, trust: dict, inline: dict[str, dict], managed: list[str] | None = None
) -> str:
    iam = aws.client("iam", region)
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Managed by TuningPad",
            Tags=aws.tags(),
        )["Role"]["Arn"]
    for pname, doc in inline.items():
        iam.put_role_policy(RoleName=name, PolicyName=pname, PolicyDocument=json.dumps(doc))
    for m in managed or []:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    return arn


def acr_role_documents(account: str, region: str, bucket: str) -> tuple[dict, dict]:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"},
                },
            }
        ],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ECRImage",
                "Effect": "Allow",
                "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                "Resource": f"arn:aws:ecr:{region}:{account}:repository/{AGENT_ECR_REPO}",
            },
            {
                "Sid": "ECRToken",
                "Effect": "Allow",
                "Action": "ecr:GetAuthorizationToken",
                "Resource": "*",
            },
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                    "logs:DescribeLogGroups",
                ],
                "Resource": (
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"
                ),
            },
            {
                "Sid": "Metrics",
                "Effect": "Allow",
                "Action": "cloudwatch:PutMetricData",
                "Resource": "*",
                "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            },
            {
                "Sid": "XRay",
                "Effect": "Allow",
                "Action": [
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                "Resource": "*",
            },
            {
                "Sid": "WorkloadIdentity",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*",
                ],
            },
            {
                "Sid": "RolloutResults",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
            {
                "Sid": "RolloutList",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{bucket}",
            },
            # Agents that call Bedrock themselves (user simulators, LLM judges).
            {
                "Sid": "BedrockInvoke",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": "*",
            },
        ],
    }
    return trust, policy


def codebuild_role_documents(account: str, region: str, bucket: str) -> tuple[dict, dict]:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "codebuild.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/tuningpad-*",
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": f"arn:aws:s3:::{bucket}/build/*",
            },
            {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                "Resource": [
                    f"arn:aws:ecr:{region}:{account}:repository/{TRAINER_ECR_REPO}",
                    f"arn:aws:ecr:{region}:{account}:repository/{AGENT_ECR_REPO}",
                ],
            },
        ],
    }
    return trust, policy


def ensure_ecr_repo(region: str, name: str) -> str:
    ecr = aws.client("ecr", region)
    try:
        repo = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
        repo = ecr.create_repository(
            repositoryName=name,
            imageScanningConfiguration={"scanOnPush": True},
            imageTagMutability="MUTABLE",
            tags=aws.tags(),
        )["repository"]
    return repo["repositoryUri"]
