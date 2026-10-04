import json
import time

from botocore.exceptions import ClientError

from app.core.db import session_scope
from app.jobs import engine as eng
from app.models import Job
from app.services import project as proj
from tests.conftest import StubClient


def _err(code, op="Op"):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _wait(job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with session_scope() as s:
            j = s.get(Job, job_id)
            if j.status in eng.TERMINAL:
                return j
        time.sleep(0.05)
    raise AssertionError("timeout")


def _setup_stubs(existing=False):
    def head_bucket(**kw):
        if not existing:
            raise _err("404", "HeadBucket")
        return {}

    def get_role(**kw):
        if not existing:
            raise _err("NoSuchEntity", "GetRole")
        return {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{kw['RoleName']}"}}

    def describe_repositories(**kw):
        if not existing:
            raise _err("RepositoryNotFoundException", "DescribeRepositories")
        name = kw["repositoryNames"][0]
        return {"repositories": [{"repositoryUri": f"1.dkr.ecr/{name}"}]}

    return {
        "sts": StubClient(get_caller_identity={"Account": "123456789012", "Arn": "arn:x"}),
        "s3": StubClient(
            head_bucket=head_bucket,
            create_bucket={},
            put_public_access_block={},
            put_bucket_encryption={},
            put_bucket_tagging={},
            put_bucket_lifecycle_configuration={},
        ),
        "iam": StubClient(
            get_role=get_role,
            update_assume_role_policy={},
            create_role=lambda **kw: {"Role": {"Arn": f"arn:aws:iam::1:role/{kw['RoleName']}"}},
            put_role_policy={},
            attach_role_policy={},
        ),
        "ecr": StubClient(
            describe_repositories=describe_repositories,
            create_repository=lambda **kw: {
                "repository": {"repositoryUri": f"new/{kw['repositoryName']}"}
            },
        ),
    }


def test_setup_region_creates_everything(stub_aws, client, monkeypatch):
    monkeypatch.setattr(
        "app.pipelines.setup.StageContext.sleep", lambda self, s: None, raising=False
    )
    monkeypatch.setattr(eng.StageContext, "sleep", lambda self, s: None)
    stubs = stub_aws(_setup_stubs(existing=False))
    r = client.post("/api/setup/region", json={"region": "us-east-1"})
    job = _wait(r.json()["job_id"])
    assert job.status == "succeeded", job.error
    res = proj.region_resources("us-east-1")
    assert res["status"] == "ready"
    assert res["bucket"] == "tuningpad-123456789012-us-east-1"
    assert res["acr_role_arn"].endswith("TuningPad-AgentRuntime-us-east-1")
    # us-east-1 must not pass a LocationConstraint
    create = [c for c in stubs["s3"].calls if c[0] == "create_bucket"][0][1]
    assert "CreateBucketConfiguration" not in create
    lifecycle = [c for c in stubs["s3"].calls if c[0] == "put_bucket_lifecycle_configuration"][0][1]
    prefixes = {r["Filter"]["Prefix"] for r in lifecycle["LifecycleConfiguration"]["Rules"]}
    assert {"smoke/", "rollouts/"} <= prefixes
    # ACR trust is scoped to this account/region
    acr = [
        c
        for c in stubs["iam"].calls
        if c[0] == "create_role" and "AgentRuntime" in c[1]["RoleName"]
    ][0]
    trust = json.loads(acr[1]["AssumeRolePolicyDocument"])
    cond = trust["Statement"][0]["Condition"]
    assert cond["StringEquals"]["aws:SourceAccount"] == "123456789012"
    view = client.get("/api/setup").json()
    assert view["regions"]["us-east-1"]["status"] == "ready"
    assert view["jobs"]["us-east-1"]["status"] == "succeeded"


def test_setup_is_idempotent(stub_aws, monkeypatch):
    monkeypatch.setattr(eng.StageContext, "sleep", lambda self, s: None)
    stubs = stub_aws(_setup_stubs(existing=True))
    job = _wait(eng.get_engine().start("setup.region", "us-west-2", {"region": "us-west-2"}))
    assert job.status == "succeeded"
    called = {c[0] for svc in stubs.values() for c in svc.calls}
    assert "create_bucket" not in called and "create_role" not in called
    assert "create_repository" not in called
    assert "update_assume_role_policy" in called  # drift corrected


def test_acr_policy_has_no_wildcard_log_groups():
    _, policy = proj.acr_role_documents("1", "us-east-1", "b")
    logs = [s for s in policy["Statement"] if s["Sid"] == "Logs"][0]
    assert logs["Resource"].endswith("/aws/bedrock-agentcore/runtimes/*")


def test_require_region_raises_before_setup():
    import pytest

    from app.core.errors import AppError

    with pytest.raises(AppError) as e:
        proj.require_region("us-east-1")
    assert e.value.code == "setup.region_not_ready"


def test_role_arn_from_assumed():
    assert (
        proj._role_arn_from_assumed("arn:aws:sts::123:assumed-role/Admin/i-abc")
        == "arn:aws:iam::123:role/Admin"
    )
