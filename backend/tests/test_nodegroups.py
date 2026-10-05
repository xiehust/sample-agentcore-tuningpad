"""EKS managed node groups (plain EC2) as a second compute pool."""

import base64
import time

import pytest
from botocore.exceptions import ClientError

from app.core.db import session_scope
from app.core.errors import AppError
from app.jobs import engine as eng
from app.models import Cluster, Job
from app.render import train as rt
from app.services import components as comp
from app.services import nodegroups as ngs
from app.services import pools
from tests.conftest import StubClient


def _nf(**kw):
    raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Op")


def test_launch_template_efa_layout_for_p5():
    d = ngs.launch_template_data("p5.48xlarge", ["sg-a", "sg-b"], efa=True, placement_group="pg")
    nics = d["NetworkInterfaces"]
    assert len(nics) == 33  # card 0: IP + EFA-only, cards 1..31: EFA-only
    assert nics[0] == {
        "NetworkCardIndex": 0,
        "DeviceIndex": 0,
        "InterfaceType": "interface",
        "Groups": ["sg-a", "sg-b"],
        "DeleteOnTermination": True,
    }
    assert {n["InterfaceType"] for n in nics[1:]} == {"efa-only"}
    assert sorted(n["NetworkCardIndex"] for n in nics[1:]) == list(range(32))
    assert all("SubnetId" not in n for n in nics)  # MNG rejects subnets in the template
    assert "SecurityGroupIds" not in d and d["Placement"] == {"GroupName": "pg"}
    assert "lustre-client" in base64.b64decode(d["UserData"]).decode()


def test_launch_template_without_efa():
    d = ngs.launch_template_data("p5.48xlarge", ["sg-a"], efa=False, placement_group=None)
    assert d["SecurityGroupIds"] == ["sg-a"] and "NetworkInterfaces" not in d
    assert d["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] == ngs.ROOT_GIB
    assert d["MetadataOptions"]["HttpTokens"] == "required"


def test_create_spot_nodegroup(stub_aws, monkeypatch):
    monkeypatch.setattr(ngs, "ensure_node_role", lambda region, name: "arn:aws:iam::1:role/node")
    eks = StubClient(
        describe_nodegroup=_nf,
        describe_cluster={"cluster": {"resourcesVpcConfig": {"clusterSecurityGroupId": "sg-eks"}}},
        create_nodegroup={},
    )
    ec2 = StubClient(
        describe_launch_templates=lambda **kw: (_ for _ in ()).throw(
            ClientError({"Error": {"Code": "InvalidLaunchTemplateName.NotFoundException"}}, "x")
        ),
        create_launch_template={
            "LaunchTemplate": {"LaunchTemplateId": "lt-1", "LatestVersionNumber": 1}
        },
    )
    stub_aws({"eks": eks, "ec2": ec2})
    out = ngs.create(
        "us-east-1",
        "eks",
        cluster_id="cl-1",
        cluster_name="dev",
        network={"cluster_sgs": ["sg-hp"], "private_subnets": ["subnet-a", "subnet-b"]},
        name="ec2-p5-spot",
        instance_type="p5.48xlarge",
        capacity="spot",
        count=1,
        max_size=1,
        efa=False,
    )
    assert out["subnets"] == ["subnet-a", "subnet-b"]  # Spot may use every AZ
    req = [c for c in eks.calls if c[0] == "create_nodegroup"][0][1]
    assert req["capacityType"] == "SPOT" and req["amiType"] == "AL2023_x86_64_NVIDIA"
    assert req["instanceTypes"] == ["p5.48xlarge"]
    # created empty, scaled up once ACTIVE
    assert req["scalingConfig"] == {"minSize": 0, "maxSize": 1, "desiredSize": 0}
    assert out["pending_count"] == 1
    assert req["labels"]["tuningpad.io/pool"] == "ec2-p5-spot"
    assert req["taints"] == [{"key": "nvidia.com/gpu", "value": "true", "effect": "NO_SCHEDULE"}]
    assert req["tags"]["TuningPadPool"] == "ec2-p5-spot" and req["tags"]["ManagedBy"]
    lt = [c for c in ec2.calls if c[0] == "create_launch_template"][0][1]
    assert lt["LaunchTemplateData"]["SecurityGroupIds"] == ["sg-hp", "sg-eks"]


def test_create_rejects_bad_input():
    with pytest.raises(AppError) as e:
        ngs.validate_name("Bad_Name")
    assert e.value.code == "nodegroup.bad_name"
    with pytest.raises(AppError) as e:
        ngs.create(
            "us-east-1",
            "eks",
            cluster_id="c",
            cluster_name="d",
            network={},
            name="ab",
            instance_type="p5.48xlarge",
            capacity="training_plan",
            count=0,
            max_size=1,
            efa=False,
        )
    assert e.value.code == "nodegroup.bad_capacity"
    with pytest.raises(AppError) as e:
        ngs.create(
            "us-east-1",
            "eks",
            cluster_id="c",
            cluster_name="d",
            network={},
            name="ab",
            instance_type="p5.4xlarge",
            capacity="spot",
            count=0,
            max_size=1,
            efa=True,
        )
    assert e.value.code == "nodegroup.no_efa"


def test_efa_picks_single_subnet(stub_aws):
    stub_aws(
        {
            "ec2": StubClient(
                describe_subnets={
                    "Subnets": [
                        {
                            "SubnetId": "a",
                            "AvailabilityZoneId": "use1-az4",
                            "AvailableIpAddressCount": 10,
                        },
                        {
                            "SubnetId": "b",
                            "AvailabilityZoneId": "use1-az5",
                            "AvailableIpAddressCount": 99,
                        },
                    ]
                }
            )
        }
    )
    net = {"private_subnets": ["a", "b"]}
    assert ngs.pick_subnets("us-east-1", net, efa=True, az_id=None) == ["b"]
    assert ngs.pick_subnets("us-east-1", net, efa=False, az_id="use1-az4") == ["a"]
    with pytest.raises(AppError):
        ngs.pick_subnets("us-east-1", net, efa=True, az_id="use1-az1")


def test_latest_failure_reports_asg_activity(stub_aws):
    stub_aws(
        {
            "autoscaling": StubClient(
                describe_scaling_activities={
                    "Activities": [
                        {
                            "StatusCode": "Failed",
                            "StatusMessage": "Could not launch Spot Instances. "
                            "InsufficientInstanceCapacity - There is no Spot capacity available",
                        },
                    ]
                }
            )
        }
    )
    ng = {"resources": {"autoScalingGroups": [{"name": "asg"}]}}
    assert "InsufficientInstanceCapacity" in ngs.latest_failure("us-east-1", ng)


def test_node_selectors():
    assert pools.node_selector("ec2", "ec2-p5") == {"tuningpad.io/pool": "ec2-p5"}
    assert pools.node_selector("hyperpod", "gpu")["sagemaker.amazonaws.com/instance-group-name"]
    assert pools.node_selector("hyperpod", "system") == {
        "sagemaker.amazonaws.com/instance-group-name": "system"
    }


def test_rayjob_uses_pool_selector():
    from app.catalog.instances import CATALOG

    job = rt.rayjob(
        run_id="r",
        name="r-a0",
        namespace="tuningpad",
        image="img",
        spec=CATALOG["p5.48xlarge"],
        nodes=2,
        group="ec2-p5",
        env=[],
        config_map="cm",
        service_account="sa",
        pvc="fsx",
        node_selector={"tuningpad.io/pool": "ec2-p5"},
    )
    rc = job["spec"]["rayClusterSpec"]
    head = rc["headGroupSpec"]["template"]["spec"]
    worker = rc["workerGroupSpecs"][0]["template"]["spec"]
    assert head["nodeSelector"] == worker["nodeSelector"] == {"tuningpad.io/pool": "ec2-p5"}
    assert head["containers"][0]["resources"]["limits"]["vpc.amazonaws.com/efa"] == 32
    sub = job["spec"]["submitterPodTemplate"]["spec"]["nodeSelector"]
    assert sub == {"sagemaker.amazonaws.com/instance-group-name": "system"}


def test_guardian_policy_covers_cluster_nodegroups():
    pol = comp.guardian_policy("b", "arn:aws:sagemaker:us-east-1:111:cluster/x", [], "eks-1")
    stmt = [s for s in pol["Statement"] if "eks:UpdateNodegroupConfig" in s["Action"]][0]
    assert stmt["Resource"] == "arn:aws:eks:us-east-1:111:nodegroup/eks-1/*/*"


def test_gpu_plugins_pinned_to_ec2_pools(monkeypatch):
    calls = []
    monkeypatch.setattr(comp.kube, "helm", lambda r, e, args: calls.append(args) or "")
    vals = []
    real = comp._helm_values

    def capture(region, eks, args, values):
        vals.append(values)
        return real(region, eks, args, values)

    monkeypatch.setattr(comp, "_helm_values", capture)
    comp.install_gpu_plugins("us-east-1", "eks")
    assert any("nvdp/nvidia-device-plugin" in a for a in calls)
    assert any("eks/aws-efa-k8s-device-plugin" in a for a in calls)
    for v in vals:
        expr = v["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        assert expr["nodeSelectorTerms"][0]["matchExpressions"][0]["key"] == "tuningpad.io/pool"


# ---------------- API + jobs ----------------


def _cluster(**kw):
    with session_scope() as s:
        s.add(
            Cluster(
                id="cl-1",
                name="dev",
                region="us-east-1",
                source="create",
                status="ready",
                hyperpod_name="tp-dev",
                eks_name="eks",
                network={"cluster_sgs": ["sg"]},
                components={},
                params={},
                idle_policy={},
                **kw,
            )
        )


HP = {
    "ClusterArn": "arn:aws:sagemaker:us-east-1:1:cluster/x",
    "ClusterStatus": "InService",
    "InstanceGroups": [{"InstanceGroupName": "gpu-p5", "InstanceType": "ml.p5.48xlarge"}],
}


def test_nodegroup_api_confirms_cost_and_unique_names(client, stub_aws, monkeypatch):
    _cluster()
    stub_aws(
        {"sagemaker": StubClient(describe_cluster=HP), "eks": StubClient(describe_nodegroup=_nf)}
    )
    monkeypatch.setattr(ngs, "price", lambda region, itype, cap, live=True: 24.0)
    body = {"name": "ec2-p5-spot", "instance_type": "p5.48xlarge", "capacity": "spot", "count": 1}
    r = client.post("/api/clusters/cl-1/nodegroups", json=body)
    assert r.json()["code"] == "cluster.confirm_cost"
    assert r.json()["detail"]["price_per_hour"] == 24.0
    r = client.post("/api/clusters/cl-1/nodegroups", json={**body, "name": "gpu-p5"})
    assert r.json()["code"] == "pool.name_taken"
    # G-family is accepted for (inference) node groups and still needs the cost confirmation
    r = client.post("/api/clusters/cl-1/nodegroups", json={**body, "instance_type": "g5.xlarge"})
    assert r.json()["code"] == "cluster.confirm_cost"
    r = client.post("/api/clusters/cl-1/nodegroups", json={**body, "instance_type": "c5.xlarge"})
    assert r.json()["code"] == "catalog.unsupported_instance"
    started = []
    monkeypatch.setattr(
        eng.JobEngine, "start", lambda self, jt, tid, p: started.append((jt, p)) or "job-x"
    )
    r = client.post("/api/clusters/cl-1/nodegroups", json={**body, "confirm_cost": True})
    assert r.json() == {"job_id": "job-x"} and started[0][0] == "cluster.nodegroup"


def _wait(job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with session_scope() as s:
            j = s.get(Job, job_id)
            if j.status in eng.TERMINAL:
                return j
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_nodegroup_job_creates_and_waits_for_gpu_nodes(stub_aws, monkeypatch):
    monkeypatch.setattr(eng.StageContext, "sleep", lambda self, s: None)
    _cluster()
    state = {"n": None, "polls": 0}

    def describe(region, eks, name):
        n = state["n"]
        if n and state.get("seen"):
            n["status"] = "ACTIVE"  # creation finished after the first poll
        state["seen"] = n is not None
        return n

    def create(region, eks, **kw):
        state["n"] = {
            "nodegroupName": kw["name"],
            "status": "CREATING",
            "scalingConfig": {"desiredSize": kw["count"]},
        }
        state["kw"] = kw
        return {"name": kw["name"], "pending_count": kw["count"]}

    def pstate(c, provider, name, want):
        state["polls"] += 1
        return {
            "current": 1 if state["polls"] > 2 else 0,
            "status": "ACTIVE",
            "why": None if state["polls"] > 2 else "Could not launch Spot Instances",
            "failed": None,
        }

    monkeypatch.setattr(ngs, "describe", describe)
    monkeypatch.setattr(ngs, "create", create)
    monkeypatch.setattr(ngs, "scale", lambda r, e, n, c: state.setdefault("scaled", []).append(c))
    monkeypatch.setattr(pools, "check_name_free", lambda c, name, provider: None)
    monkeypatch.setattr(pools, "state", pstate)
    monkeypatch.setattr("app.pipelines.cluster.refresh_guardian", lambda cid: None)
    jid = eng.get_engine().start(
        "cluster.nodegroup",
        "cl-1",
        {"name": "ec2-p5-spot", "instance_type": "p5.48xlarge", "capacity": "spot", "count": 1},
    )
    job = _wait(jid)
    assert job.status == "succeeded", job.error
    assert state["kw"]["capacity"] == "spot" and state["kw"]["efa"] is False
    assert state["scaled"] == [1]  # desired raised once the empty group was ACTIVE


def test_run_on_ec2_pool_scales_and_waits(monkeypatch):
    from app.pipelines import run as pr

    calls = []
    monkeypatch.setattr(pr.pools, "find", lambda c, name: None)
    monkeypatch.setattr(pr.pools, "ensure", lambda c, **kw: calls.append(kw) or "created")
    seq = iter(
        [
            {"current": 0, "status": "CREATING", "why": None, "failed": None},
            {"current": 1, "status": "ACTIVE", "why": None, "failed": None},
        ]
    )
    monkeypatch.setattr(pr.pools, "state", lambda c, p, n, w: next(seq))
    monkeypatch.setattr(pr.pc, "refresh_guardian", lambda cid: None)
    monkeypatch.setattr(pr, "save_run", lambda rid, **kw: None)

    class Ctx:
        def log(self, m):
            pass

        def detail(self, m):
            pass

        def wait_until(self, fn, **kw):
            while not fn():
                pass

    run = {
        "id": "run-1",
        "compute": {
            "provider": "ec2",
            "instance_group": "ec2-p5-spot",
            "instance_type": "p5.48xlarge",
            "nodes": 1,
            "capacity": "spot",
        },
    }
    pr._capacity_ec2(Ctx(), run, {"id": "cl-1"})
    assert calls == [
        {
            "provider": "ec2",
            "name": "ec2-p5-spot",
            "instance_type": "p5.48xlarge",
            "count": 1,
            "capacity": "spot",
            "efa": False,
        }
    ]


def test_ec2_pool_rejects_training_plan(client):
    body = {
        "name": "r",
        "agent_runtime_id": "rt-x",
        "train_dataset_id": "d",
        "model_id": "m",
        "compute": {
            "instance_group": "ec2-p5",
            "instance_type": "p5.48xlarge",
            "provider": "ec2",
            "capacity": "training_plan",
        },
    }
    r = client.post("/api/runs/preview", json=body)
    assert r.status_code == 422


def test_asg_found_by_tag_while_creating(stub_aws):
    asg = StubClient(
        describe_auto_scaling_groups={
            "AutoScalingGroups": [
                {"AutoScalingGroupName": "eks-x", "Instances": [{"LifecycleState": "Pending"}]}
            ]
        }
    )
    stub_aws({"autoscaling": asg})
    ng = {"nodegroupName": "ec2-p5", "clusterName": "eks", "resources": {}}
    assert ngs.asg_names("us-east-1", ng) == ["eks-x"]
    assert ngs.asg_counts("us-east-1", ng) == {"running": 1, "in_service": 0}


def test_cluster_delete_removes_vpc_runtimes_and_waits_for_enis(stub_aws, monkeypatch):
    from app.models import Agent, AgentRuntime
    from app.pipelines import cluster as pc

    _cluster(cfn_stack=None)
    with session_scope() as s:
        s.get(Cluster, "cl-1").network = {"private_subnets": ["subnet-a"]}
        s.add(Agent(id="ag-1", name="a", source="template", config={}, status="ready", checks={}))
        s.flush()
        s.add(
            AgentRuntime(
                id="rt-1",
                agent_id="ag-1",
                cluster_id="cl-1",
                region="us-east-1",
                runtime_id="tp_x-1",
                status="ready",
            )
        )
    enis = iter(
        [{"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1"}]}, {"NetworkInterfaces": []}]
    )
    acc = StubClient(delete_agent_runtime={})
    stub_aws(
        {
            "bedrock-agentcore-control": acc,
            "ec2": StubClient(describe_network_interfaces=lambda **kw: next(enis)),
        }
    )

    class Ctx:
        target_id = "cl-1"
        context: dict = {}

        def set(self, k, v):
            self.context = {**self.context, k: v}

        def log(self, m):
            pass

        def detail(self, m):
            pass

        def wait_until(self, fn, **kw):
            while not fn():
                pass

    ctx = Ctx()
    pc.stage_delete_runtimes(ctx)
    assert acc.calls == [("delete_agent_runtime", {"agentRuntimeId": "tp_x-1"})]
    assert ctx.context["runtimes_deleted"] == {"rt-1": True}  # a retry skips it
    with session_scope() as s:
        assert s.get(AgentRuntime, "rt-1").status == "deleted"


def test_cluster_delete_removes_platform_sgs_before_the_stack(stub_aws, monkeypatch):
    """sg-acr / sg-nlb sit in the stack's VPC: deleting them only after the stack left the
    VPC undeletable (DELETE_FAILED). In-use SGs are retried once the stack is gone."""
    from botocore.exceptions import ClientError

    from app.pipelines import cluster as pc

    _cluster(cfn_stack="tuningpad-dev")
    with session_scope() as s:
        s.get(Cluster, "cl-1").network = {"cluster_sgs": [], "sg_acr": "sg-a", "sg_nlb": "sg-n"}
    order: list[str] = []
    in_use = {"sg-n": 1}  # busy on the first attempt only

    def delete_sg(GroupId):
        order.append(f"sg:{GroupId}")
        if in_use.get(GroupId):
            in_use[GroupId] -= 1
            raise ClientError({"Error": {"Code": "DependencyViolation"}}, "DeleteSecurityGroup")
        return {}

    stack = {"alive": True}

    def delete_stack(StackName):
        order.append("stack")
        stack["alive"] = False
        return {}

    stub_aws(
        {
            "ec2": StubClient(delete_security_group=delete_sg),
            "cloudformation": StubClient(delete_stack=delete_stack),
        }
    )
    monkeypatch.setattr(
        pc.hp,
        "stack_status",
        lambda r, n: {"status": "DELETE_IN_PROGRESS"} if stack["alive"] else None,
    )

    class Ctx:
        target_id = "cl-1"

        def log(self, m):
            pass

        def detail(self, m):
            pass

        def sleep(self, s):
            pass

        def wait_until(self, fn, **kw):
            while not fn():
                pass

    # sg-n busy once: retried (wait loop) before the stack delete is issued
    pc.stage_delete_cluster(Ctx())
    assert order == ["sg:sg-a", "sg:sg-n", "sg:sg-n", "stack"]


def test_stack_failure_reason_ignores_earlier_operations(stub_aws):
    from app.services import hyperpod as hp

    sid = "arn:stack/x"

    def ev(lid, status, reason="", rtype="AWS::EC2::VPC", phys="p"):
        return {
            "LogicalResourceId": lid,
            "ResourceStatus": status,
            "ResourceStatusReason": reason,
            "ResourceType": rtype,
            "PhysicalResourceId": phys,
            "StackId": sid,
        }

    stack = {"rtype": "AWS::CloudFormation::Stack", "phys": sid}
    events = [  # newest first: 2nd delete failed on VPC; 1st on a subnet
        ev("VPCStack", "DELETE_FAILED", "VPC failed"),
        ev("x", "DELETE_IN_PROGRESS", **stack),
        ev("PrivateSubnetStack", "DELETE_FAILED", "subnet failed"),
        ev("x", "DELETE_IN_PROGRESS", **stack),
    ]
    stub_aws({"cloudformation": StubClient(describe_stack_events={"StackEvents": events})})
    assert hp.stack_failure_reason("us-east-1", "x") == "VPCStack: VPC failed"
