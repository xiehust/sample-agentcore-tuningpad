import json
import time
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.core.db import session_scope
from app.core.errors import AppError
from app.jobs import engine as eng
from app.models import Cluster, Job
from app.services import components as comp
from app.services import hyperpod as hp
from app.services import project as proj
from tests.conftest import StubClient

FIXTURES = Path(__file__).parent / "fixtures"


def _wait(job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with session_scope() as s:
            j = s.get(Job, job_id)
            if j.status in eng.TERMINAL:
                return j
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_cfn_parameters_exist_in_official_template():
    known = set(json.loads((FIXTURES / "hyperpod_main_stack_params.json").read_text()))
    params = hp.cfn_parameters(name="rl-dev", az_ids=["use1-az4", "use1-az6"], tags={"a": "b"})
    keys = {p["ParameterKey"] for p in params}
    assert keys <= known, keys - known
    by = {p["ParameterKey"]: p["ParameterValue"] for p in params}
    groups = json.loads(by["InstanceGroupSettings1"])
    assert groups == [
        {
            "InstanceGroupName": "system",
            "InstanceType": "ml.m5.2xlarge",
            "InstanceCount": 1,
            "ThreadsPerCore": 1,
        }
    ]
    assert by["NodeProvisioningMode"] == "Continuous"
    assert by["EnableHPInferenceFeature"] == "false"
    assert by["FsxAvailabilityZoneId"] == "use1-az4"
    assert json.loads(by["Tags"]) == [{"Key": "a", "Value": "b"}]


@pytest.mark.parametrize(
    "cap,ok", [(1200, True), (2400, True), (4800, True), (1800, False), (600, False)]
)
def test_fsx_capacity_rules(cap, ok):
    if ok:
        hp.cfn_parameters(name="abc", az_ids=["x"], fsx_capacity_gib=cap)
    else:
        with pytest.raises(AppError):
            hp.cfn_parameters(name="abc", az_ids=["x"], fsx_capacity_gib=cap)


@pytest.mark.parametrize("name,ok", [("rl-dev", True), ("a", False), ("Bad", False), ("9x", False)])
def test_cluster_name(name, ok):
    if ok:
        hp.validate_name(name)
    else:
        with pytest.raises(AppError):
            hp.validate_name(name)


HP_DESC = {
    "ClusterArn": "arn:aws:sagemaker:us-east-1:1:cluster/abc",
    "ClusterStatus": "InService",
    "NodeProvisioningMode": "Continuous",
    "Orchestrator": {"Eks": {"ClusterArn": "arn:aws:eks:us-east-1:1:cluster/tp-x-eks"}},
    "VpcConfig": {"SecurityGroupIds": ["sg-cluster"], "Subnets": ["subnet-a"]},
    "InstanceGroups": [
        {
            "InstanceGroupName": "system",
            "InstanceType": "ml.m5.2xlarge",
            "CurrentCount": 1,
            "TargetCount": 1,
            "ExecutionRole": "arn:aws:iam::1:role/exec",
            "LifeCycleConfig": {"SourceS3Uri": "s3://lcs", "OnCreate": "on_create.sh"},
            "ThreadsPerCore": 1,
            "Status": "InService",
        }
    ],
}


def test_group_spec_new_and_existing():
    spec = hp.group_spec(
        HP_DESC, name="gpu", instance_type="p5.48xlarge", count=2, deep_health_checks=True
    )
    assert spec["InstanceType"] == "ml.p5.48xlarge"
    assert spec["ExecutionRole"] == "arn:aws:iam::1:role/exec"
    assert spec["LifeCycleConfig"]["OnCreate"] == "on_create.sh"
    assert spec["OnStartDeepHealthChecks"] == ["InstanceStress", "InstanceConnectivity"]
    spot = hp.group_spec(HP_DESC, name="gpu", instance_type="p5.48xlarge", count=1, capacity="spot")
    assert spot["CapacityRequirements"] == {"Spot": {}}
    with pytest.raises(AppError):
        hp.group_spec(
            HP_DESC, name="gpu", instance_type="p5.48xlarge", count=1, capacity="training_plan"
        )
    resized = hp.group_spec(
        HP_DESC, name="system", instance_type="", count=0, existing=HP_DESC["InstanceGroups"][0]
    )
    assert resized["InstanceCount"] == 0 and resized["InstanceType"] == "ml.m5.2xlarge"
    assert "CurrentCount" not in resized


def test_spot_needs_continuous():
    desc = {**HP_DESC, "NodeProvisioningMode": None}
    with pytest.raises(AppError) as e:
        hp.group_spec(desc, name="g", instance_type="p5.48xlarge", count=1, capacity="spot")
    assert e.value.code == "cluster.spot_needs_continuous"


def test_security_groups_rules_are_idempotent(stub_aws):
    created = []

    def describe_sgs(**kw):
        return {"SecurityGroups": []}

    def create_sg(**kw):
        created.append(kw["GroupName"])
        return {"GroupId": f"sg-{kw['GroupName']}"}

    rules = []

    def authorize(**kw):
        key = (
            kw["GroupId"],
            kw["IpPermissions"][0]["FromPort"],
            kw["IpPermissions"][0]["UserIdGroupPairs"][0]["GroupId"],
        )
        if key in rules:
            raise ClientError(
                {"Error": {"Code": "InvalidPermission.Duplicate", "Message": ""}}, "A"
            )
        rules.append(key)

    stub_aws(
        {
            "ec2": StubClient(
                describe_security_groups=describe_sgs,
                create_security_group=create_sg,
                authorize_security_group_ingress=authorize,
            )
        }
    )
    net = {"vpc_id": "vpc-1", "cluster_sgs": ["sg-cluster"]}
    out = comp.ensure_security_groups("us-east-1", "dev", net)
    comp.ensure_security_groups("us-east-1", "dev", net)  # duplicates swallowed
    assert out == {"sg_acr": "sg-tp-dev-acr", "sg_nlb": "sg-tp-dev-nlb"}
    assert ("sg-cluster", 18765, "sg-tp-dev-acr") in rules
    assert ("sg-cluster", 8000, "sg-tp-dev-nlb") in rules
    assert ("sg-tp-dev-nlb", 8000, "sg-tp-dev-acr") in rules
    assert not any(r[1] == 22 for r in rules)


def test_irsa_trust_scoped_to_service_account():
    t = comp.irsa_trust(
        "arn:aws:iam::1:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABC",
        "tuningpad",
        "tuningpad-workload",
    )
    cond = t["Statement"][0]["Condition"]["StringEquals"]
    assert (
        cond["oidc.eks.us-east-1.amazonaws.com/id/ABC:sub"]
        == "system:serviceaccount:tuningpad:tuningpad-workload"
    )


def test_guardian_policy_is_cluster_scoped():
    pol = comp.guardian_policy("b", "arn:aws:sagemaker:us-east-1:1:cluster/abc", ["arn:role/exec"])
    sm = pol["Statement"][0]
    assert sm["Resource"] == "arn:aws:sagemaker:us-east-1:1:cluster/abc"
    assert "sagemaker:UpdateCluster" in sm["Action"]
    assert {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::b"} in pol[
        "Statement"
    ]
    passrole = pol["Statement"][-1]
    assert passrole["Condition"]["StringEquals"]["iam:PassedToService"] == "sagemaker.amazonaws.com"


def test_guardian_cronjob_runs_on_system_group():
    cj = comp.guardian_cronjob()
    spec = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"sagemaker.amazonaws.com/instance-group-name": "system"}
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"


def _no_nodegroup(**kw):
    raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Op")


def _cluster_row(**kw):
    with session_scope() as s:
        s.add(
            Cluster(
                id="cl-1",
                name="dev",
                region="us-east-1",
                source="create",
                status="ready",
                hyperpod_name="tp-dev",
                eks_name="tp-dev-eks",
                hyperpod_arn=HP_DESC["ClusterArn"],
                **{
                    "network": {},
                    "components": {},
                    "params": {},
                    "idle_policy": {"idle_minutes": 20, "budget_usd": 500},
                    **kw,
                },
            )
        )


def test_scale_job_updates_group_and_waits(stub_aws, monkeypatch):
    monkeypatch.setattr(eng.StageContext, "sleep", lambda self, s: None)
    _cluster_row()
    state = {"count": 0, "calls": 0}

    def describe(**kw):
        state["calls"] += 1
        groups = list(HP_DESC["InstanceGroups"])
        if state.get("added"):
            groups.append(
                {
                    "InstanceGroupName": "gpu",
                    "InstanceType": "ml.p5.48xlarge",
                    "CurrentCount": 2 if state["calls"] > 3 else 0,
                    "TargetCount": 2,
                    "Status": "Updating",
                    "ExecutionRole": "r",
                    "LifeCycleConfig": {},
                }
            )
        return {**HP_DESC, "InstanceGroups": groups}

    def update(**kw):
        state["added"] = True
        state["spec"] = kw["InstanceGroups"][0]

    sm = StubClient(describe_cluster=describe, update_cluster=update)
    stub_aws({"sagemaker": sm, "eks": StubClient(describe_nodegroup=_no_nodegroup)})
    monkeypatch.setattr("app.pipelines.cluster.refresh_guardian", lambda cid: None)
    jid = eng.get_engine().start(
        "cluster.scale",
        "cl-1",
        {"group": "gpu", "instance_type": "p5.48xlarge", "count": 2, "capacity": "on_demand"},
    )
    job = _wait(jid)
    assert job.status == "succeeded", job.error
    assert state["spec"]["InstanceCount"] == 2
    assert state["spec"]["InstanceType"] == "ml.p5.48xlarge"


def test_scale_rejects_non_p_family(stub_aws, monkeypatch):
    _cluster_row()
    stub_aws({"sagemaker": StubClient(describe_cluster=HP_DESC, update_cluster={})})
    job = _wait(
        eng.get_engine().start(
            "cluster.scale", "cl-1", {"group": "gpu", "instance_type": "g6e.12xlarge", "count": 1}
        )
    )
    assert job.status == "failed" and job.error_code == "catalog.unsupported_instance"


def test_scale_endpoint_requires_cost_confirmation(client, stub_aws):
    _cluster_row()
    stub_aws({"sagemaker": StubClient(describe_cluster=HP_DESC)})
    r = client.post(
        "/api/clusters/cl-1/groups",
        json={"group": "gpu", "instance_type": "p5.48xlarge", "count": 1},
    )
    assert r.status_code == 400 and r.json()["code"] == "cluster.confirm_cost"
    r = client.post("/api/clusters/cl-1/groups", json={"group": "system", "count": 0})
    assert r.json()["code"] == "cluster.system_group"


def test_delete_requires_name(client):
    _cluster_row()
    assert client.delete("/api/clusters/cl-1").json()["code"] == "cluster.confirm_name"


def test_create_requires_region_setup(client):
    r = client.post("/api/clusters", json={"name": "dev", "az_ids": ["use1-az4"]})
    assert r.status_code == 409 and r.json()["code"] == "setup.region_not_ready"


def test_create_starts_job_when_ready(client, monkeypatch):
    proj.save_region("us-east-1", {"status": "ready", "bucket": "b"})
    started = []
    monkeypatch.setattr(
        eng.JobEngine,
        "start",
        lambda self, t, target=None, payload=None, **k: started.append((t, target)) or "job-x",
    )
    r = client.post(
        "/api/clusters", json={"name": "dev", "region": "us-east-1", "az_ids": ["use1-az4"]}
    )
    assert r.status_code == 200
    assert started == [("cluster.create", r.json()["id"])]
    r = client.post(
        "/api/clusters", json={"name": "dev", "region": "us-east-1", "az_ids": ["use1-az4"]}
    )
    assert r.json()["code"] == "cluster.name_taken"


PLAN_ARN = "arn:aws:sagemaker:us-east-1:111122223333:training-plan/ftp-p5"


def _plan(**kw):
    p = {
        "TrainingPlanArn": PLAN_ARN,
        "TrainingPlanName": "ftp-p5",
        "Status": "Active",
        "TotalInstanceCount": 2,
        "AvailableInstanceCount": 2,
        "InUseInstanceCount": 0,
        "TargetResources": ["hyperpod-cluster"],
        "ReservedCapacitySummaries": [
            {"InstanceType": "ml.p5.48xlarge", "AvailabilityZoneId": "use1-az4"}
        ],
    }
    p.update(kw)
    return p


@pytest.mark.parametrize(
    "plan_kw,itype,count,code",
    [
        ({}, "p5.48xlarge", 2, None),
        ({"Status": "Expired"}, "p5.48xlarge", 1, "plan.not_usable"),
        ({"TargetResources": ["training-job"]}, "p5.48xlarge", 1, "plan.wrong_target"),
        ({}, "p4d.24xlarge", 1, "plan.instance_mismatch"),
        ({}, "p5.48xlarge", 3, "plan.too_small"),
        (
            {
                "ReservedCapacitySummaries": [
                    {"InstanceType": "ml.p5.48xlarge", "AvailabilityZoneId": "use1-az6"}
                ]
            },
            "p5.48xlarge",
            1,
            "plan.az_not_in_cluster",
        ),
    ],
)
def test_check_plan(stub_aws, plan_kw, itype, count, code):
    stub_aws({"sagemaker": StubClient(describe_training_plan=_plan(**plan_kw))})
    if code is None:
        p = hp.check_plan("us-east-1", PLAN_ARN, itype, count, ["use1-az4", "use1-az5"])
        assert p["instance_type"] == "p5.48xlarge" and p["az_id"] == "use1-az4"
        return
    with pytest.raises(AppError) as e:
        hp.check_plan("us-east-1", PLAN_ARN, itype, count, ["use1-az4", "use1-az5"])
    assert e.value.code == code


def test_plan_arn_format_and_region():
    with pytest.raises(AppError) as e:
        hp.describe_plan("us-east-1", "ftp-p5")
    assert e.value.code == "plan.bad_arn"
    with pytest.raises(AppError) as e:
        hp.describe_plan("us-east-2", PLAN_ARN)
    assert e.value.code == "plan.region_mismatch"


def test_existing_group_capacity_mismatch():
    g = {"InstanceGroupName": "gpu-p5", "InstanceType": "ml.p5.48xlarge"}
    hp.check_existing_capacity(g, "on_demand", None)
    hp.check_existing_capacity(None, "training_plan", PLAN_ARN)
    with pytest.raises(AppError) as e:
        hp.check_existing_capacity(g, "training_plan", PLAN_ARN)
    assert e.value.code == "cluster.group_capacity_mismatch"
    planned = {**g, "TrainingPlanArn": PLAN_ARN}
    hp.check_existing_capacity(planned, "training_plan", PLAN_ARN)
    with pytest.raises(AppError):
        hp.check_existing_capacity(planned, "training_plan", PLAN_ARN.replace("p5", "other"))


def test_scale_with_training_plan_is_prepaid(client, stub_aws):
    _cluster_row(network={"az_ids": ["use1-az4"]})
    stub_aws({"sagemaker": StubClient(describe_cluster=HP_DESC, describe_training_plan=_plan())})
    body = {
        "group": "gpu-ftp",
        "instance_type": "p5.48xlarge",
        "count": 1,
        "capacity": "training_plan",
    }
    assert client.post("/api/clusters/cl-1/groups", json=body).json()["code"] == "plan.required"
    r = client.post("/api/clusters/cl-1/groups", json={**body, "training_plan_arn": PLAN_ARN})
    assert r.json()["code"] == "cluster.confirm_cost"
    assert r.json()["detail"] == {"price_per_hour": 0, "prepaid": True}
    r = client.get(
        "/api/training-plans/check",
        params={"arn": PLAN_ARN, "cluster_id": "cl-1", "instance_type": "p5.48xlarge", "count": 1},
    )
    assert r.status_code == 200 and r.json()["available"] == 2


def test_list_plans_normalized(stub_aws):
    stub_aws({"sagemaker": StubClient(list_training_plans={"TrainingPlanSummaries": [_plan()]})})
    [p] = hp.list_plans("us-east-1")
    assert (p["instance_type"], p["az_id"], p["available"]) == ("p5.48xlarge", "use1-az4", 2)
