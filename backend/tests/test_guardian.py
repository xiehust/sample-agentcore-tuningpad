"""The in-cluster guardian runs without the backend; test its decisions offline."""

import importlib.util
import io
import json
import time

import pytest

from app.core.config import REPO_ROOT


@pytest.fixture
def guardian(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "guardian", REPO_ROOT / "cluster_assets/guardian.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self, ledger=None):
        self.ledger = ledger
        self.put = None

    def get_object(self, Bucket, Key):
        if self.ledger is None:
            raise self.exceptions.NoSuchKey()
        return {"Body": io.BytesIO(json.dumps(self.ledger).encode())}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put = json.loads(Body)


class FakeSM:
    def __init__(self, count):
        self.count = count
        self.updates = []

    def describe_cluster(self, ClusterName):
        return {
            "InstanceGroups": [
                {"InstanceGroupName": "system", "InstanceType": "ml.m5.2xlarge", "CurrentCount": 1},
                {
                    "InstanceGroupName": "gpu",
                    "InstanceType": "ml.p5.48xlarge",
                    "CurrentCount": self.count,
                    "TargetCount": self.count,
                    "ExecutionRole": "r",
                    "LifeCycleConfig": {"x": 1},
                },
            ]
        }

    def update_cluster(self, ClusterName, InstanceGroups):
        self.updates.append(InstanceGroups[0])


def _run(guardian, monkeypatch, tmp_path, *, pods, ledger=None, count=2, budget=0, idle=30):
    cfg = {
        "region": "us-east-1",
        "cluster": "tp-dev",
        "bucket": "b",
        "ledger_key": "k",
        "budget_usd": budget,
        "groups": {"gpu": {"idle_minutes": idle, "price_per_hour": 66.0}},
    }
    (tmp_path / "guardian.json").write_text(json.dumps(cfg))
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(
            tmp_path / "guardian.json" if p == "/config/guardian.json" else p, *a, **k
        ),
    )
    nodes = {"items": [{"metadata": {"name": "n1", "labels": {guardian.GROUP_LABEL: "gpu"}}}]}
    monkeypatch.setattr(
        guardian, "k8s", lambda path: nodes if path.endswith("/nodes") else {"items": pods}
    )
    s3, sm = FakeS3(ledger), FakeSM(count)
    monkeypatch.setattr(
        guardian.boto3, "client", lambda svc, region_name: {"s3": s3, "sagemaker": sm}[svc]
    )
    guardian.main()
    return s3, sm


RUNNING = [{"status": {"phase": "Running"}, "spec": {"nodeName": "n1"}}]
PENDING_FOR_GPU = [
    {
        "status": {"phase": "Pending"},
        "spec": {"nodeSelector": {"sagemaker.amazonaws.com/instance-group-name": "gpu"}},
    }
]


def test_busy_group_is_left_alone(guardian, monkeypatch, tmp_path):
    s3, sm = _run(guardian, monkeypatch, tmp_path, pods=RUNNING)
    assert sm.updates == [] and s3.put["groups"]["gpu"]["idle_since"] is None


def test_pending_pod_counts_as_busy(guardian, monkeypatch, tmp_path):
    old = time.time() - 3600
    ledger = {
        "groups": {"gpu": {"node_hours": 0, "cost_usd": 0, "idle_since": old}},
        "updated_at_epoch": time.time() - 300,
    }
    s3, sm = _run(guardian, monkeypatch, tmp_path, pods=PENDING_FOR_GPU, ledger=ledger)
    assert sm.updates == []


def test_idle_group_scaled_to_zero_after_threshold(guardian, monkeypatch, tmp_path):
    ledger = {
        "groups": {"gpu": {"node_hours": 0, "cost_usd": 0, "idle_since": time.time() - 31 * 60}},
        "updated_at_epoch": time.time() - 300,
    }
    s3, sm = _run(guardian, monkeypatch, tmp_path, pods=[], ledger=ledger)
    assert len(sm.updates) == 1
    assert sm.updates[0]["InstanceCount"] == 0
    assert sm.updates[0]["LifeCycleConfig"] == {"x": 1}  # full group spec preserved
    assert s3.put["actions"][-1]["reason"].startswith("idle")


def test_first_idle_observation_only_starts_timer(guardian, monkeypatch, tmp_path):
    s3, sm = _run(guardian, monkeypatch, tmp_path, pods=[])
    assert sm.updates == [] and s3.put["groups"]["gpu"]["idle_since"] is not None


def test_budget_scales_even_busy_groups(guardian, monkeypatch, tmp_path):
    ledger = {
        "groups": {"gpu": {"node_hours": 10, "cost_usd": 999, "idle_since": None}},
        "updated_at_epoch": time.time() - 300,
    }
    s3, sm = _run(guardian, monkeypatch, tmp_path, pods=RUNNING, ledger=ledger, budget=1000)
    assert sm.updates and sm.updates[0]["InstanceCount"] == 0
    assert "budget" in s3.put["alert"]
    # 2 nodes × 5 min × $66/h ≈ $11 accrued
    assert s3.put["groups"]["gpu"]["cost_usd"] == pytest.approx(
        999 + 2 * (300 / 3600) * 66, rel=0.01
    )


class FakeEKS:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, desired, status="ACTIVE"):
        self.desired, self.status, self.updates = desired, status, []

    def describe_nodegroup(self, clusterName, nodegroupName):
        return {
            "nodegroup": {
                "scalingConfig": {"desiredSize": self.desired, "maxSize": 2, "minSize": 0},
                "status": self.status,
                "resources": {"autoScalingGroups": [{"name": "asg-1"}]},
            }
        }

    def update_nodegroup_config(self, **kw):
        self.updates.append(kw)


class FakeASG:
    def __init__(self, running):
        self.running = running

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        inst = [{"LifecycleState": "InService"}] * self.running
        return {"AutoScalingGroups": [{"Instances": inst}]}


def _run_ng(guardian, monkeypatch, tmp_path, *, pods, ledger=None, desired=1, status="ACTIVE"):
    cfg = {
        "region": "us-east-1",
        "cluster": "tp-dev",
        "bucket": "b",
        "ledger_key": "k",
        "budget_usd": 0,
        "groups": {},
        "eks_cluster": "eks",
        "nodegroups": {"ec2-p5": {"idle_minutes": 30, "price_per_hour": 24.0}},
    }
    (tmp_path / "guardian.json").write_text(json.dumps(cfg))
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(
            tmp_path / "guardian.json" if p == "/config/guardian.json" else p, *a, **k
        ),
    )
    nodes = {"items": [{"metadata": {"name": "n9", "labels": {guardian.POOL_LABEL: "ec2-p5"}}}]}
    monkeypatch.setattr(
        guardian, "k8s", lambda path: nodes if path.endswith("/nodes") else {"items": pods}
    )
    s3, sm, eks = FakeS3(ledger), FakeSM(0), FakeEKS(desired, status)
    clients = {"s3": s3, "sagemaker": sm, "eks": eks, "autoscaling": FakeASG(desired)}
    monkeypatch.setattr(guardian.boto3, "client", lambda svc, region_name: clients[svc])
    guardian.main()
    return s3, eks


def test_idle_ec2_nodegroup_scaled_to_zero_and_costed(guardian, monkeypatch, tmp_path):
    ledger = {
        "groups": {"ec2-p5": {"node_hours": 0, "cost_usd": 0, "idle_since": time.time() - 3600}},
        "updated_at_epoch": time.time() - 3600,
    }
    s3, eks = _run_ng(guardian, monkeypatch, tmp_path, pods=[], ledger=ledger)
    assert eks.updates == [
        {
            "clusterName": "eks",
            "nodegroupName": "ec2-p5",
            "scalingConfig": {"minSize": 0, "maxSize": 2, "desiredSize": 0},
        }
    ]
    st = s3.put["groups"]["ec2-p5"]
    assert st["provider"] == "ec2" and 23 < st["cost_usd"] < 25  # 1 node × 1 h × $24


def test_ec2_nodegroup_busy_by_pool_label(guardian, monkeypatch, tmp_path):
    ledger = {
        "groups": {"ec2-p5": {"node_hours": 0, "cost_usd": 0, "idle_since": time.time() - 3600}},
        "updated_at_epoch": time.time() - 300,
    }
    pending = [
        {"status": {"phase": "Pending"}, "spec": {"nodeSelector": {"tuningpad.io/pool": "ec2-p5"}}}
    ]
    s3, eks = _run_ng(guardian, monkeypatch, tmp_path, pods=pending, ledger=ledger)
    assert eks.updates == [] and s3.put["groups"]["ec2-p5"]["idle_since"] is None
    s3, eks = _run_ng(guardian, monkeypatch, tmp_path, pods=RUNNING_N9, ledger=ledger)
    assert eks.updates == []


RUNNING_N9 = [{"status": {"phase": "Running"}, "spec": {"nodeName": "n9"}}]


def test_ec2_nodegroup_mid_update_is_retried_later(guardian, monkeypatch, tmp_path):
    ledger = {
        "groups": {"ec2-p5": {"node_hours": 0, "cost_usd": 0, "idle_since": time.time() - 3600}},
        "updated_at_epoch": time.time() - 300,
    }
    s3, eks = _run_ng(guardian, monkeypatch, tmp_path, pods=[], ledger=ledger, status="UPDATING")
    assert eks.updates == []
