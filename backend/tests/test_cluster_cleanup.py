"""Cluster deletion frees compute while managed AgentCore networking is reclaimed."""

from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from kubernetes.client.exceptions import ApiException

from app.core.db import session_scope, utcnow
from app.core.errors import AppError
from app.jobs import engine as eng
from app.models import Agent, AgentRuntime, Cluster, Job
from app.pipelines import cluster as pc
from app.services import hyperpod as hp
from app.services import kube
from tests.conftest import StubClient

STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/tuningpad-clean/original"


class Context:
    target_id = "cl-clean"
    payload = {}

    def __init__(self, context=None):
        self.context = dict(context or {})
        self.logs = []

    def set(self, key, value):
        self.context[key] = value

    def log(self, message):
        self.logs.append(message)

    def defer(self, **kwargs):
        raise eng.JobDeferred(**kwargs)


@pytest.fixture
def cluster():
    with session_scope() as session:
        session.add(
            Cluster(
                id="cl-clean",
                name="clean",
                region="us-east-1",
                source="create",
                status="deleting",
                cfn_stack="tuningpad-clean",
                network={
                    "private_subnets": ["subnet-a"],
                    "vpc_id": "vpc-own",
                    "cluster_sgs": [],
                    "sg_acr": "sg-acr",
                },
            )
        )
    return Context()


def _resource(logical, kind, physical, status="CREATE_COMPLETE", reason=None):
    return {
        "LogicalResourceId": logical,
        "ResourceType": kind,
        "PhysicalResourceId": physical,
        "ResourceStatus": status,
        "ResourceStatusReason": reason,
    }


def _stack(status):
    return {"Stacks": [{"StackStatus": status, "StackId": STACK_ID}]}


def _error(code, message="", operation="Example"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def _fail(code, message=""):
    def fail(**kwargs):
        raise _error(code, message)

    return fail


def _runtime(*, obs=True):
    with session_scope() as session:
        session.add(
            Agent(id="agent-clean", name="clean", source="template", status="ready", config={})
        )
        session.flush()
        session.add(
            AgentRuntime(
                id="runtime-clean",
                agent_id="agent-clean",
                cluster_id="cl-clean",
                region="us-east-1",
                runtime_id="runtime-main",
                status="ready",
                obs={"runtime_id": "runtime-obs", "status": "ready"} if obs else None,
            )
        )


def test_runtime_requests_are_durable_and_observation_twin_is_reconciled(cluster, stub_aws):
    _runtime()
    ctl = StubClient(
        list_agent_runtimes={
            "agentRuntimes": [
                {"agentRuntimeId": "runtime-main", "status": "READY"},
                {"agentRuntimeId": "runtime-obs", "status": "DELETING"},
            ]
        },
        delete_agent_runtime={},
    )
    stub_aws({"bedrock-agentcore-control": ctl})
    pc.stage_delete_runtimes(cluster)
    assert [kw for name, kw in ctl.calls if name == "delete_agent_runtime"] == [
        {"agentRuntimeId": "runtime-main"}
    ]
    assert cluster.context["runtimes_deleted"] == {
        "runtime-clean": True,
        "runtime-clean:obs": True,
    }
    with session_scope() as session:
        runtime = session.get(AgentRuntime, "runtime-clean")
        assert runtime.status == "deleting"
        assert runtime.obs["status"] == "deleting"
    # A backend restart reuses accepted markers, including legacy ones, without another API.
    calls = list(ctl.calls)
    pc.stage_delete_runtimes(Context(cluster.context))
    assert ctl.calls == calls

    ctl._methods["list_agent_runtimes"] = {"agentRuntimes": []}
    stub_aws(
        {
            "bedrock-agentcore-control": ctl,
            "ec2": StubClient(
                describe_network_interfaces={"NetworkInterfaces": []}, delete_security_group={}
            ),
        }
    )
    pc.stage_delete_network(cluster)
    with session_scope() as session:
        runtime = session.get(AgentRuntime, "runtime-clean")
        assert runtime.status == "deleted"
        assert runtime.obs == {"runtime_id": "runtime-obs", "status": "deleted"}


def test_runtime_absence_covers_crash_before_accepted_marker(cluster, stub_aws):
    _runtime(obs=False)
    ctl = StubClient(list_agent_runtimes={"agentRuntimes": []})
    stub_aws({"bedrock-agentcore-control": ctl})
    pc.stage_delete_runtimes(cluster)
    assert cluster.context["runtimes_deleted"] == {"runtime-clean": True}
    assert "agentcore_cleanup_started_at" not in cluster.context
    with session_scope() as session:
        assert session.get(AgentRuntime, "runtime-clean").status == "deleted"


def test_runtime_listing_pages_and_does_not_start_eni_clock_while_busy(cluster, stub_aws):
    _runtime(obs=False)

    def page(**kwargs):
        if kwargs.get("nextToken") == "second":
            return {"agentRuntimes": [{"agentRuntimeId": "runtime-main", "status": "CREATING"}]}
        return {"agentRuntimes": [], "nextToken": "second"}

    ctl = StubClient(list_agent_runtimes=page)
    stub_aws({"bedrock-agentcore-control": ctl})
    with pytest.raises(eng.JobDeferred) as deferred:
        pc.stage_delete_runtimes(cluster)
    assert deferred.value.reason == "clusterRuntimeDeleting"
    assert "agentcore_cleanup_started_at" not in cluster.context
    assert ctl.calls[-1][1] == {"maxResults": 100, "nextToken": "second"}


def test_accepted_runtime_is_not_marked_deleted_while_still_listed(cluster, stub_aws):
    _runtime(obs=False)
    ctl = StubClient(
        list_agent_runtimes={
            "agentRuntimes": [{"agentRuntimeId": "runtime-main", "status": "DELETING"}]
        }
    )
    stub_aws(
        {
            "bedrock-agentcore-control": ctl,
            "ec2": StubClient(describe_network_interfaces={"NetworkInterfaces": []}),
        }
    )
    with pytest.raises(eng.JobDeferred) as deferred:
        pc.stage_delete_network(cluster)
    assert deferred.value.reason == "clusterRuntimeDeleting"
    with session_scope() as session:
        assert session.get(AgentRuntime, "runtime-clean").status != "deleted"


@pytest.mark.parametrize(
    ("failed_id", "failed_row"),
    [("runtime-main", "runtime-clean"), ("runtime-obs", "runtime-clean:obs")],
)
def test_failed_runtime_deletion_is_terminal_and_only_explicit_retry_resubmits(
    cluster, stub_aws, failed_id, failed_row
):
    _runtime()
    cluster.context["runtimes_deleted"] = {"runtime-clean": True, "runtime-clean:obs": True}
    ctl = StubClient(
        list_agent_runtimes={
            "agentRuntimes": [
                {
                    "agentRuntimeId": rid,
                    "status": "DELETE_FAILED" if rid == failed_id else "DELETING",
                }
                for rid in ("runtime-main", "runtime-obs")
            ]
        },
        delete_agent_runtime={},
    )
    # No EC2 stub: runtime failure must be reported before a normal ENI wait.
    stub_aws({"bedrock-agentcore-control": ctl})
    with pytest.raises(AppError) as error:
        pc.stage_delete_network(cluster)
    assert error.value.code == "cluster.delete_failed"
    assert failed_id in error.value.message
    assert cluster.context["runtime_delete_retry"] == [failed_row]
    assert failed_row not in cluster.context["runtimes_deleted"]
    assert not any(name == "delete_agent_runtime" for name, _ in ctl.calls)
    restored = Context(cluster.context)
    with pytest.raises(eng.JobDeferred) as deferred:
        pc.stage_delete_network(restored)
    assert deferred.value.reason == "clusterRuntimeDeleting"
    assert [kwargs for name, kwargs in ctl.calls if name == "delete_agent_runtime"] == [
        {"agentRuntimeId": failed_id}
    ]
    assert restored.context["runtime_delete_retry"] == []
    # A repeated service failure is terminal again, never an automatic delete loop.
    with pytest.raises(AppError):
        pc.stage_delete_network(restored)
    assert restored.context["runtime_delete_retry"] == [failed_row]
    assert sum(name == "delete_agent_runtime" for name, _ in ctl.calls) == 1


def test_explicit_retry_reissues_failed_runtime_from_succeeded_runtime_stage(
    cluster, stub_aws, monkeypatch
):
    _runtime(obs=False)
    with session_scope() as session:
        session.get(Cluster, "cl-clean").source = "import"
    state = {"runtime": "READY", "delete_calls": 0}

    def delete(**kwargs):
        state["delete_calls"] += 1
        state["runtime"] = "DELETE_FAILED" if state["delete_calls"] == 1 else "DELETING"
        return {}

    def runtime_list(**kwargs):
        return {
            "agentRuntimes": [{"agentRuntimeId": "runtime-main", "status": state["runtime"]}]
            if state["runtime"]
            else []
        }

    stub_aws(
        {
            "bedrock-agentcore-control": StubClient(
                list_agent_runtimes=runtime_list, delete_agent_runtime=delete
            )
        }
    )
    eng.register(
        "test.cluster.runtime-delete",
        [
            eng.Stage("runtimes", pc.stage_delete_runtimes),
            eng.Stage("network", pc.stage_delete_network),
        ],
        on_finish=pc._on_deleted,
    )
    engine = eng.get_engine()
    job_id = engine.start("test.cluster.runtime-delete", cluster.target_id, run_inline=True)
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed"
        assert job.error_code == "cluster.delete_failed"
        assert job.stages[0]["status"] == "succeeded"
    assert engine.resume_due() == []
    assert state["delete_calls"] == 1
    monkeypatch.setattr(engine, "_submit", lambda jid: engine._execute(jid))
    engine.retry(job_id)
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.status == "waiting"
        assert job.stages[0]["status"] == "succeeded"
        assert job.stages[1]["status"] == "waiting"
    assert state["delete_calls"] == 2
    state["runtime"] = None
    with session_scope() as session:
        session.get(Job, job_id).next_attempt_at = utcnow() - timedelta(seconds=1)
    engine.resume_due()
    with session_scope() as session:
        assert session.get(Job, job_id).status == "succeeded"
        assert session.get(AgentRuntime, "runtime-clean").status == "deleted"


def test_managed_eni_pages_remain_waiting_after_eight_hours(cluster, stub_aws, monkeypatch):
    started = datetime(2026, 10, 19, 10, tzinfo=UTC)
    cluster.context["agentcore_cleanup_started_at"] = started.isoformat()
    cluster.context["delete_subnets"] = ["subnet-a", "subnet-b"]
    monkeypatch.setattr(pc, "utcnow", lambda: started + timedelta(hours=9))

    def interfaces(**kwargs):
        assert kwargs["IncludeManagedResources"] is True
        assert kwargs["Filters"] == [
            {"Name": "subnet-id", "Values": ["subnet-a", "subnet-b"]},
            {"Name": "interface-type", "Values": ["agentic_ai"]},
        ]
        if kwargs.get("NextToken"):
            return {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-b"}]}
        return {
            "NetworkInterfaces": [{"NetworkInterfaceId": "eni-a"}],
            "NextToken": "second",
        }

    ec2 = StubClient(describe_network_interfaces=interfaces)
    stub_aws({"ec2": ec2})
    with pytest.raises(eng.JobDeferred) as deferred:
        pc.stage_delete_network(cluster)
    assert deferred.value.reason == "clusterAgentcoreEnisDelayed"
    assert deferred.value.detail_params == {"eni_count": 2, "waited_hours": 9.0}
    assert deferred.value.delay_s == 60
    assert len(cluster.logs) == 1
    restored = Context(cluster.context)
    with pytest.raises(eng.JobDeferred):
        pc.stage_delete_network(restored)
    assert restored.logs == []
    assert restored.context["agentcore_cleanup_started_at"] == started.isoformat()


def test_workloads_wait_for_finalizers_without_allowing_service_recreation(cluster, monkeypatch):
    with session_scope() as session:
        session.get(Cluster, "cl-clean").eks_name = "eks-own"
    state = {"namespace_deleting": False, "load_balancer_gone": False}
    calls = []

    def services(*args):
        if state["namespace_deleting"] and state["load_balancer_gone"]:
            return []
        metadata = {"name": "head"}
        if state["namespace_deleting"]:
            metadata["deletionTimestamp"] = "2026-10-19T00:00:00Z"
        return [{"metadata": metadata}]

    def delete(region, eks, version, kind, name, **kwargs):
        calls.append((kind, name))
        if kind == "Namespace":
            state["namespace_deleting"] = True

    monkeypatch.setattr(kube, "list_objects", services)
    monkeypatch.setattr(kube, "delete", delete)
    with pytest.raises(eng.JobDeferred) as deferred:
        pc.stage_delete_workloads(cluster)
    assert deferred.value.reason == "clusterWorkloadsDeleting"
    assert calls == [("Service", "head"), ("Namespace", kube.NAMESPACE)]
    assert not cluster.context.get("workloads_cleanup_verified")
    # Restart during finalization: neither delete is submitted again.
    restored = Context(cluster.context)
    with pytest.raises(eng.JobDeferred):
        pc.stage_delete_workloads(restored)
    assert len(calls) == 2
    state["load_balancer_gone"] = True
    pc.stage_delete_workloads(restored)
    assert restored.context["workloads_cleanup_verified"] is True


def test_workloads_recheck_services_created_between_list_and_namespace_delete(cluster, monkeypatch):
    with session_scope() as session:
        session.get(Cluster, "cl-clean").eks_name = "eks-own"
    snapshots = iter(
        [
            [],
            [{"metadata": {"name": "late-service", "deletionTimestamp": "2026-10-19T00:00:00Z"}}],
            [],
        ]
    )
    deletes = []
    monkeypatch.setattr(kube, "list_objects", lambda *args: next(snapshots))
    monkeypatch.setattr(kube, "delete", lambda *args, **kwargs: deletes.append(args[3:5]))
    with pytest.raises(eng.JobDeferred):
        pc.stage_delete_workloads(cluster)
    assert deletes == [("Namespace", kube.NAMESPACE)]
    assert not cluster.context.get("workloads_cleanup_verified")
    with pytest.raises(eng.JobDeferred) as deferred:
        pc.stage_delete_workloads(cluster)
    assert deferred.value.detail_params == {"service_count": 1}
    assert not cluster.context.get("workloads_cleanup_verified")
    pc.stage_delete_workloads(cluster)
    assert cluster.context["workloads_cleanup_verified"] is True
    assert len(deletes) == 1


def test_workload_access_error_prevents_early_root_delete(cluster, stub_aws, monkeypatch):
    with session_scope() as session:
        session.get(Cluster, "cl-clean").eks_name = "eks-own"
    cfn = StubClient(describe_stacks=_stack("CREATE_COMPLETE"))
    stub_aws({"cloudformation": cfn})

    def denied(*args):
        raise ApiException(status=403)

    monkeypatch.setattr(kube, "list_objects", denied)
    with pytest.raises(ApiException):
        pc.stage_delete_compute(cluster)
    assert [name for name, _ in cfn.calls] == ["describe_stacks"]
    assert not cluster.context.get("workloads_cleanup_verified")


def test_revoke_removes_only_our_sg_pairs_and_propagates_permission_errors(cluster, stub_aws):
    with session_scope() as session:
        session.get(Cluster, "cl-clean").network = {
            "cluster_sgs": ["sg-cluster"],
            "sg_acr": "sg-acr",
        }
    ec2 = StubClient(
        describe_security_groups={
            "SecurityGroups": [
                {
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 18765,
                            "ToPort": 18765,
                            "UserIdGroupPairs": [
                                {"GroupId": "sg-acr"},
                                {"GroupId": "sg-unrelated"},
                            ],
                            "IpRanges": [{"CidrIp": "10.0.0.0/24"}],
                        }
                    ]
                }
            ]
        },
        revoke_security_group_ingress={},
    )
    stub_aws({"ec2": ec2})
    pc._revoke_platform_sg_rules(cluster, ec2, pc.load(cluster.target_id))
    assert ec2.calls[-1] == (
        "revoke_security_group_ingress",
        {
            "GroupId": "sg-cluster",
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 18765,
                    "ToPort": 18765,
                    "UserIdGroupPairs": [{"GroupId": "sg-acr"}],
                }
            ],
        },
    )
    ec2._methods["describe_security_groups"] = _fail("UnauthorizedOperation")
    with pytest.raises(ClientError):
        pc._revoke_platform_sg_rules(Context(), ec2, pc.load(cluster.target_id))


@pytest.mark.parametrize("code", ["UnauthorizedOperation", "AccessDenied", "RequestLimitExceeded"])
def test_sg_cleanup_does_not_convert_unexpected_errors_into_waits(cluster, stub_aws, code):
    ec2 = StubClient(delete_security_group=_fail(code))
    stub_aws({"ec2": ec2})
    with pytest.raises(ClientError):
        pc._delete_platform_sgs(cluster, ec2, pc.load(cluster.target_id))


def test_platform_sg_dependency_defers_and_not_found_completes(cluster, stub_aws):
    ec2 = StubClient(
        describe_network_interfaces={"NetworkInterfaces": []},
        delete_security_group=_fail("DependencyViolation"),
    )
    stub_aws({"ec2": ec2})
    with pytest.raises(eng.JobDeferred) as deferred:
        pc.stage_delete_network(cluster)
    assert deferred.value.reason == "clusterSecurityGroups"
    ec2._methods["delete_security_group"] = _fail("InvalidGroup.NotFound")
    pc.stage_delete_network(cluster)
    assert cluster.context["platform_sgs_remaining"] == []


def test_resumed_cleanup_restores_cluster_deleting_state(cluster, stub_aws):
    with session_scope() as session:
        session.get(Cluster, "cl-clean").status = "delete_failed"
    stub_aws(
        {
            "ec2": StubClient(
                describe_network_interfaces={"NetworkInterfaces": [{"NetworkInterfaceId": "eni-a"}]}
            )
        }
    )
    with pytest.raises(eng.JobDeferred):
        pc.stage_delete_network(cluster)
    with session_scope() as session:
        assert session.get(Cluster, "cl-clean").status == "deleting"


def test_import_does_not_delete_or_inspect_shared_cloud_infrastructure(cluster):
    with session_scope() as session:
        session.get(Cluster, "cl-clean").source = "import"
    # No AWS clients are stubbed: touching any shared infrastructure fails the test.
    pc.stage_delete_compute(cluster)
    pc.stage_delete_network(cluster)
    pc.stage_delete_cluster(cluster)
    assert "delete_stack_id" not in cluster.context


def test_recursive_stack_inventory_pages_and_resolves_current_leaf_failures(stub_aws):
    nested_id = "arn:stack/network"
    resource = _resource(
        "Subnet",
        "AWS::EC2::Subnet",
        "subnet-b",
        "DELETE_FAILED",
        "The subnet 'subnet-b' has dependencies and cannot be deleted.",
    )

    def resources(StackName, **kwargs):
        if StackName == nested_id:
            return {"StackResourceSummaries": [resource]}
        if kwargs.get("NextToken"):
            return {
                "StackResourceSummaries": [
                    _resource(
                        "DeletedCompute",
                        "AWS::CloudFormation::Stack",
                        "arn:gone",
                        "DELETE_COMPLETE",
                    )
                ]
            }
        return {
            "StackResourceSummaries": [
                _resource("Network", "AWS::CloudFormation::Stack", nested_id, "DELETE_FAILED")
            ],
            "NextToken": "second",
        }

    cfn = StubClient(list_stack_resources=resources)
    stub_aws({"cloudformation": cfn})
    tree = hp.stack_resources("us-east-1", STACK_ID)
    failures = hp.stack_delete_failures(tree)
    assert failures == [{**resource, "stack_id": nested_id}]
    assert hp.network_delete_dependency(failures[0])
    assert not any(kwargs["StackName"] == "arn:gone" for _, kwargs in cfn.calls)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("AWS::IAM::Role", "DependencyViolation"),
        ("AWS::FSx::FileSystem", "has dependencies and cannot be deleted"),
        ("AWS::SageMaker::Cluster", "DependencyViolation"),
        ("AWS::EC2::Subnet", "AccessDenied: unable to check DependencyViolation"),
        ("AWS::EC2::SecurityGroup", "invalid identifier"),
        ("AWS::CloudFormation::Stack", "The subnet has dependencies and cannot be deleted"),
    ],
)
def test_only_explicit_network_dependency_failures_are_waitable(cluster, stub_aws, kind, reason):
    resource = _resource("Blocked", kind, "resource-own", "DELETE_FAILED", reason)
    cfn = StubClient(
        describe_stacks=_stack("DELETE_FAILED"),
        list_stack_resources={
            "StackResourceSummaries": [resource] if kind != "AWS::CloudFormation::Stack" else []
        },
    )
    stub_aws({"cloudformation": cfn})
    if kind == "AWS::CloudFormation::Stack":
        # An unresolved nested failure must remain a real failure.
        assert not hp.network_delete_dependency(resource)
    with pytest.raises(AppError) as error:
        pc.stage_delete_cluster(cluster)
    assert error.value.code == "cluster.delete_failed"
    assert not any(name == "delete_stack" for name, _ in cfn.calls)


def test_stack_access_denied_is_never_inferred_as_absence(stub_aws):
    stub_aws(
        {
            "cloudformation": StubClient(
                describe_stacks=_fail("AccessDenied", "stack does not exist or access denied")
            )
        }
    )
    with pytest.raises(ClientError):
        hp.stack_status("us-east-1", STACK_ID)


def test_compute_stays_waiting_when_network_dependency_still_holds_compute(cluster, stub_aws):
    cfn = StubClient(
        describe_stacks=_stack("DELETE_FAILED"),
        list_stack_resources={
            "StackResourceSummaries": [
                _resource("HyperPod", "AWS::SageMaker::Cluster", "hp-own"),
                _resource(
                    "Subnet",
                    "AWS::EC2::Subnet",
                    "subnet-a",
                    "DELETE_FAILED",
                    "The subnet 'subnet-a' has dependencies and cannot be deleted.",
                ),
            ]
        },
        delete_stack={},
    )
    ec2 = StubClient(
        delete_security_group=_fail("DependencyViolation"),
        describe_network_interfaces={"NetworkInterfaces": [{"NetworkInterfaceId": "eni-a"}]},
    )
    stub_aws({"cloudformation": cfn, "ec2": ec2})
    with pytest.raises(eng.JobDeferred) as deferred:
        pc.stage_delete_compute(cluster)
    assert deferred.value.reason == "clusterAgentcoreEnis"
    assert not any(name == "delete_stack" for name, _ in cfn.calls)
    ec2._methods.update(
        delete_security_group={}, describe_network_interfaces={"NetworkInterfaces": []}
    )
    with pytest.raises(eng.JobDeferred) as deferred:
        pc.stage_delete_compute(Context(cluster.context))
    assert deferred.value.reason == "clusterComputeDeleting"
    assert cfn.calls[-1] == ("delete_stack", {"StackName": STACK_ID})


def test_cluster_cleanup_resumes_after_restart_and_delayed_enis_without_retry(
    cluster, stub_aws, monkeypatch
):
    """The full engine pipeline releases compute first and completes the same waiting job."""
    state = {"status": "CREATE_COMPLETE", "compute_gone": False, "eni_count": 2}
    delete_calls = []
    first_wait = utcnow()
    monkeypatch.setattr(pc, "utcnow", lambda: first_wait)

    def describe(StackName):
        if state["status"] is None:
            raise _error("ValidationError", f"Stack with id {StackName} does not exist")
        return _stack(state["status"])

    def resources(StackName, **kwargs):
        return {
            "StackResourceSummaries": [
                _resource(
                    "HyperPod",
                    "AWS::SageMaker::Cluster",
                    "hp-own",
                    "DELETE_COMPLETE" if state["compute_gone"] else "CREATE_COMPLETE",
                ),
                _resource(
                    "Subnet",
                    "AWS::EC2::Subnet",
                    "subnet-b",
                    "DELETE_FAILED" if state["status"] == "DELETE_FAILED" else "CREATE_COMPLETE",
                    "The subnet 'subnet-b' has dependencies and cannot be deleted.",
                ),
            ]
        }

    def delete(StackName):
        delete_calls.append(StackName)
        state["status"] = "DELETE_IN_PROGRESS"
        return {}

    def delete_sg(**kwargs):
        if state["eni_count"]:
            raise _error("DependencyViolation")
        return {}

    def enis(**kwargs):
        assert kwargs["Filters"][0]["Values"] == ["subnet-a", "subnet-b"]
        assert kwargs["IncludeManagedResources"] is True
        return {
            "NetworkInterfaces": [
                {"NetworkInterfaceId": f"eni-{i}"} for i in range(state["eni_count"])
            ]
        }

    stub_aws(
        {
            "cloudformation": StubClient(
                describe_stacks=describe, list_stack_resources=resources, delete_stack=delete
            ),
            "ec2": StubClient(delete_security_group=delete_sg, describe_network_interfaces=enis),
        }
    )
    engine = eng.get_engine()
    job_id = engine.start("cluster.delete", cluster.target_id, run_inline=True)

    def waiting_stage():
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job.status == "waiting"
            assert job.error_code is None
            assert job.next_attempt_at is not None
            assert session.get(Cluster, cluster.target_id).status == "deleting"
            return next(stage for stage in job.stages if stage["status"] == "waiting")

    def wake(current_engine):
        with session_scope() as session:
            session.get(Job, job_id).next_attempt_at = utcnow() - timedelta(seconds=1)
        monkeypatch.setattr(current_engine, "_submit", lambda jid: current_engine._execute(jid))
        assert current_engine.resume_due() == [job_id]

    assert waiting_stage()["name"] == "compute"
    assert delete_calls == [STACK_ID]  # requested even though AgentCore ENIs still exist
    wake(engine)
    assert waiting_stage()["name"] == "compute"
    assert len(delete_calls) == 1  # no duplicate while CloudFormation is deleting

    state.update(status="DELETE_FAILED", compute_gone=True)
    wake(engine)
    assert waiting_stage()["name"] == "network"
    with session_scope() as session:
        captured = session.get(Job, job_id).context["agentcore_cleanup_started_at"]

    # Restart after nine hours: preserve the wait clock, automatically wake, keep waiting.
    engine.shutdown()
    restarted = eng.JobEngine(max_workers=1)
    eng.set_engine(restarted)
    monkeypatch.setattr(pc, "utcnow", lambda: first_wait + timedelta(hours=9))
    wake(restarted)
    assert waiting_stage()["reason"] == "clusterAgentcoreEnisDelayed"
    with session_scope() as session:
        assert session.get(Job, job_id).context["agentcore_cleanup_started_at"] == captured

    state["eni_count"] = 0
    wake(restarted)
    assert waiting_stage()["name"] == "cluster"
    assert delete_calls == [STACK_ID, STACK_ID]  # normal root retry after blockers clear
    state["status"] = None
    wake(restarted)
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.status == "succeeded"
        assert all(stage["status"] == "succeeded" for stage in job.stages)
        assert session.get(Cluster, cluster.target_id).status == "deleted"
