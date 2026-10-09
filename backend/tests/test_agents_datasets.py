import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from app.core.errors import AppError
from app.pipelines import agent as pa
from app.services import agents as svc
from app.services import datasets as ds
from app.templates_lib import list_templates, resolve_params

# ---------------- templates ----------------


def test_templates_load():
    ids = {t["id"] for t in list_templates()}
    assert {"gsm8k_math", "officebench"} <= ids


def test_template_params_validation():
    p = resolve_params("gsm8k_math", {"reward_method": "flexible"})
    assert p["reward_method"] == "flexible" and p["format_score"] == 0.0
    with pytest.raises(AppError):
        resolve_params("gsm8k_math", {"reward_method": "fuzzy"})
    with pytest.raises(AppError):
        resolve_params("gsm8k_math", {"format_score": 3})
    with pytest.raises(AppError):
        resolve_params("gsm8k_math", {"system_prompt": "  "})


def test_template_context_contains_config_and_toolkit_files(monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "toolkit_wheel", lambda dest: (dest / "dist").mkdir() or dest)
    agent = {
        "id": "ag-1",
        "source": "template",
        "template_id": "gsm8k_math",
        "config": {"params": {"reward_method": "flexible"}},
    }
    ctx = svc.prepare_context(agent)
    names = {p.name for p in ctx.iterdir()}
    assert {"rl_app.py", "reward.py", "models.py", "Dockerfile", "tp_config.json"} <= names
    # entrypoint: ADOT only for the eval-only runtime (TP_OBSERVABILITY=1)
    assert "tp_entry.sh" in names and "tp_entry.sh" in (ctx / "Dockerfile").read_text()
    assert "exec python -m rl_app" in (ctx / "tp_entry.sh").read_text()
    cfg = json.loads((ctx / "tp_config.json").read_text())
    assert cfg["reward_method"] == "flexible"
    checks = svc.static_checks(ctx)
    assert checks["errors"] == [] and checks["warnings"] == []


def test_static_checks_catch_contract_violations(tmp_path):
    (tmp_path / "app.py").write_text("from bedrock_agentcore import BedrockAgentCoreApp\n")
    r = svc.static_checks(tmp_path)
    assert any("Dockerfile" in e for e in r["errors"])
    assert any("AgentCoreRLApp" in e for e in r["errors"])
    assert any("api_key" in w for w in r["warnings"])


def test_snapshot_unsafe_import_time_calls_are_flagged(tmp_path):
    src = (
        "import uuid, random, time, datetime\n"
        "RUN_ID = uuid.uuid4().hex\n"
        "SEED = random.random()\n"
        "STARTED = datetime.datetime.now()\n"
        "def handler():\n"
        "    return uuid.uuid4(), time.time()\n"  # per request: fine
        "class A:\n"
        "    def f(self):\n"
        "        return random.randint(0, 9)\n"
        "if __name__ == '__main__':\n"
        "    print(time.time())\n"
    )
    assert svc.snapshot_unsafe_calls(src) == [
        (2, "uuid.uuid4"),
        (3, "random.random"),
        (4, "datetime.now"),
    ]
    assert svc.snapshot_unsafe_calls("def broken(:\n") == []
    (tmp_path / "Dockerfile").write_text("EXPOSE 8080\n")
    (tmp_path / "app.py").write_text(src)
    w = svc.static_checks(tmp_path)["warnings"]
    assert sum("Runtime V2" in x for x in w) == 3 and any(x.startswith("app.py:2:") for x in w)


def test_templates_are_snapshot_safe():
    """The shipped templates deploy on V2 by default: no import-time unsafe calls."""
    from app.core.config import get_settings
    from app.templates_lib import get_template, template_dir

    for tid in ("gsm8k_math", "officebench"):
        t = get_template(tid)
        files = list(template_dir(tid).glob("agent/*.py"))
        tk = get_settings().toolkit_path / t["toolkit_dir"]
        files += [tk / f for f in t.get("toolkit_files", []) if f.endswith(".py")]
        for f in files:
            assert svc.snapshot_unsafe_calls(f.read_text()) == [], f


def test_zip_extraction_rejects_traversal(tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../evil.py", "x")
    with pytest.raises(AppError) as e:
        svc.safe_extract_zip(z, tmp_path / "out")
    assert e.value.code == "agent.bad_zip"


def test_zip_single_folder_is_unwrapped(tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("myagent/Dockerfile", "FROM x")
        zf.writestr("myagent/app.py", "")
    out = tmp_path / "out"
    out.mkdir()
    svc.safe_extract_zip(z, out)
    assert (out / "Dockerfile").exists()


def test_runtime_name_is_acr_safe():
    n = svc.runtime_name("gsm8k-math", "rl-dev")
    assert n == "tp_gsm8k_math_rl_dev"
    assert len(svc.runtime_name("a" * 60, "b")) <= 48


def test_network_config():
    assert svc.network_config("PUBLIC") == {"networkMode": "PUBLIC"}
    v = svc.network_config("VPC", ["s1"], ["sg"])
    assert v["networkModeConfig"] == {"subnets": ["s1"], "securityGroups": ["sg"]}
    with pytest.raises(AppError):
        svc.network_config("VPC", [], [])


@pytest.mark.parametrize(
    "rewards,ok",
    [
        (1, True),
        (0.5, True),
        ([0, 1.0], True),
        (None, False),
        ("1", False),
        (True, False),
        ([], False),
    ],
)
def test_numeric_reward(rewards, ok):
    assert svc._numeric_reward(rewards) is ok


def test_smoke_uses_api_key_override_and_smoke_prefix(monkeypatch):
    captured = {}

    class Fut:
        def __init__(self, r):
            self.r = r

        def result(self, timeout):
            return self.r

    class Client:
        def __init__(self, **kw):
            captured["init"] = kw

        def invoke(self, payload, input_id, api_key):
            captured.setdefault("keys", []).append(api_key)
            return Fut({"status_code": 200, "rewards": 1.0})

    monkeypatch.setattr(svc, "rollout_client", lambda **kw: Client(**kw))
    monkeypatch.setattr(svc, "bedrock_openai", lambda region: ("https://br/openai/v1", "tok"))
    out = svc.smoke("us-east-1", "arn:rt", "bucket", "ag-1", [{"prompt": "q"}], lambda m: None)
    assert out["ok"] and out["passed"] == 1
    assert captured["init"]["exp_id"] == "smoke/ag-1"
    # RolloutClient overwrites payload['_rollout'] — the key must go through invoke(api_key=)
    assert captured["keys"] == ["tok"]


def test_parse_image_uri():
    assert svc.parse_image_uri("123456789012.dkr.ecr.us-east-1.amazonaws.com/repo:tag") == (
        "123456789012",
        "us-east-1",
        "repo",
        "tag",
    )
    with pytest.raises(AppError):
        svc.parse_image_uri("docker.io/library/python:3")


def test_smoke_payloads_precedence():
    agent = {"source": "template", "template_id": "gsm8k_math", "config": {}}
    assert len(pa.smoke_payloads(agent, None)) == 2
    assert pa.smoke_payloads(agent, [{"prompt": "x"}]) == [{"prompt": "x"}]
    custom = {"source": "upload", "template_id": None, "config": {"smoke_payloads": [{"a": 1}]}}
    assert pa.smoke_payloads(custom, None) == [{"a": 1}]


def test_data_driven_smoke_uses_the_runtimes_region():
    """OfficeBench payloads are S3 URIs the runtime role can read only in its own Region:
    a newer dataset in another Region must not be picked (AccessDenied in the smoke)."""
    from datetime import UTC, datetime, timedelta

    from app.core.db import session_scope
    from app.models import Dataset

    now = datetime.now(UTC)
    with session_scope() as s:
        for did, region, age in (("ds-e1", "us-east-1", 2), ("ds-e2", "us-east-2", 1)):
            s.add(
                Dataset(
                    id=did,
                    name=did,
                    region=region,
                    source="builtin:officebench",
                    template_id="officebench",
                    status="ready",
                    splits={},
                    sample=[{"payload": {"task_uri": f"s3://{region}/t"}}] * 2,
                    stats={},
                    created_at=now - timedelta(hours=age),
                )
            )
    agent = {"source": "template", "template_id": "officebench", "config": {}}
    assert pa.smoke_payloads(agent, None, "us-east-1")[0]["task_uri"] == "s3://us-east-1/t"
    assert pa.smoke_payloads(agent, None)[0]["task_uri"] == "s3://us-east-2/t"  # newest overall


def test_agent_create_validation(client):
    r = client.post(
        "/api/agents", json={"name": "Bad Name", "source": "template", "template_id": "gsm8k_math"}
    )
    assert r.json()["code"] == "agent.invalid_name"
    r = client.post(
        "/api/agents", json={"name": "x1", "source": "image", "image_uri": "docker.io/x"}
    )
    assert r.json()["code"] == "agent.bad_image_uri"


# ---------------- datasets ----------------


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def test_read_jsonl_and_validate(tmp_path):
    p = _write(
        tmp_path,
        "d.jsonl",
        '{"prompt": "1+1?", "answer": "2"}\n\n{"prompt": "2+2?", "answer": "4"}\n',
    )
    rows = ds.read_rows(p, "d.jsonl")
    assert rows[0] == {"payload": {"prompt": "1+1?", "answer": "2"}}
    stats = ds.validate_rows(rows, prompt_field="prompt", required=["answer"])
    assert stats["rows"] == 2 and stats["fields"]["answer"] == 2


def test_read_csv_and_payload_column(tmp_path):
    p = _write(tmp_path, "d.csv", "prompt,answer\nq1,1\nq2,2\n")
    assert ds.read_rows(p, "d.csv")[1]["payload"] == {"prompt": "q2", "answer": "2"}
    p2 = _write(
        tmp_path,
        "w.jsonl",
        json.dumps(
            {"payload": {"task_uri": "s3://b/k"}, "prompt": [{"role": "user", "content": "t"}]}
        ),
    )
    rows = ds.read_rows(p2, "w.jsonl")
    ds.validate_rows(rows, prompt_field=None, explicit_prompt=True)


def test_read_parquet_numpy_values(tmp_path):
    p = tmp_path / "d.parquet"
    pd.DataFrame([{"payload": {"prompt": "q", "answer": "1"}}]).to_parquet(p)
    rows = ds.read_rows(p, "d.parquet")
    assert rows == [{"payload": {"prompt": "q", "answer": "1"}}]


@pytest.mark.parametrize(
    "rows,code_part",
    [
        ([{"payload": {"prompt": 5}}], "must be a string"),
        ([{"payload": {"prompt": "q", "_rollout": {}}}], "reserved key"),
        ([{"payload": {"prompt": "q"}}], "missing required field 'answer'"),
        ([], None),
    ],
)
def test_validation_errors(rows, code_part):
    with pytest.raises(AppError) as e:
        ds.validate_rows(rows, prompt_field="prompt", required=["answer"])
    if code_part:
        assert code_part in json.dumps(e.value.detail)


def test_bad_format(tmp_path):
    p = _write(tmp_path, "d.txt", "x")
    with pytest.raises(AppError) as e:
        ds.read_rows(p, "d.txt")
    assert e.value.code == "dataset.bad_format"


def test_split_deterministic():
    rows = [{"payload": {"prompt": str(i)}} for i in range(100)]
    a = ds.split_rows(rows, 0.1)
    b = ds.split_rows(rows, 0.1)
    assert a == b and len(a[1]) == 10 and len(a[0]) == 90


def test_write_split_shape(tmp_path, stub_aws):
    from tests.conftest import StubClient

    s3 = StubClient(upload_file=None)
    stub_aws({"s3": s3})
    out = ds.write_split("ds-1", "train", [{"payload": {"prompt": "q"}}], "us-east-1", "b")
    assert out["s3_key"] == "datasets/ds-1/train.parquet"
    df = pd.read_parquet(ds.local_dir("ds-1") / "train.parquet")
    assert list(df.columns) == ["payload"]  # PayloadDataset contract: single payload column


def test_upload_endpoint_rejects_invalid_rows(client, monkeypatch):
    from app.jobs import engine as eng
    from app.services import project as proj

    proj.save_region("us-east-1", {"status": "ready", "bucket": "b"})
    import time

    files = {"train": ("t.jsonl", io.BytesIO(b'{"prompt": 1}\n'), "application/json")}
    r = client.post(
        "/api/datasets/upload", data={"name": "bad", "region": "us-east-1"}, files=files
    )
    assert r.status_code == 200
    jid = r.json()["job_id"]
    from app.core.db import session_scope
    from app.models import Job

    for _ in range(100):
        with session_scope() as s:
            j = s.get(Job, jid)
            if j.status in eng.TERMINAL:
                break
        time.sleep(0.05)
    assert j.status == "failed" and j.error_code == "dataset.invalid"
    d = client.get(f"/api/datasets/{r.json()['id']}").json()
    assert d["status"] == "failed"


def test_supported_az_parse():
    r = (
        "The following subnets are in unsupported availability zones in region us-east-1: "
        "subnet-1 in us-east-1f (ID: use1-az5). Supported availability zones are: use1-az4, use1-az1, use1-az2"
    )
    assert svc.supported_az_ids(r) == ["use1-az4", "use1-az1", "use1-az2"]
    assert svc.supported_az_ids("other") == []


# ---------------- AgentCore Runtime platform version ----------------


def test_resolve_platform_version(monkeypatch):
    assert svc.resolve_platform_version("us-west-2", "auto") == "V2"
    assert svc.resolve_platform_version("ap-southeast-1", "auto") == "V1"
    assert svc.resolve_platform_version("ap-southeast-1", "V1") == "V1"
    assert svc.resolve_platform_version("us-east-1", None) == "V2"  # settings default: auto
    with pytest.raises(AppError) as e:
        svc.resolve_platform_version("ap-southeast-1", "V2")
    assert e.value.code == "agent.platform_unsupported_region"
    with pytest.raises(AppError) as e:
        svc.resolve_platform_version("us-east-1", "V3")
    assert e.value.code == "agent.bad_platform_version"


def test_deploy_runtime_sends_platform_version_on_create_and_update(stub_aws):
    from tests.conftest import StubClient

    ctl = StubClient(
        list_agent_runtimes={"agentRuntimes": []},
        create_agent_runtime={"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1"},
        update_agent_runtime={"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1"},
    )
    stub_aws({"bedrock-agentcore-control": ctl})
    net = svc.network_config("PUBLIC")
    svc.deploy_runtime("us-east-1", "n", "img", "role", net, None, "V2")
    svc.deploy_runtime("us-east-1", "n", "img", "role", net, "rt-1", "V1")
    calls = {name: kw for name, kw in ctl.calls if name != "list_agent_runtimes"}
    assert calls["create_agent_runtime"]["platformVersion"] == "V2"
    assert calls["update_agent_runtime"]["platformVersion"] == "V1"
    assert calls["update_agent_runtime"]["agentRuntimeId"] == "rt-1"


class _Ctx:
    def __init__(self, target_id, payload):
        self.target_id, self.payload, self.context, self.logs = target_id, payload, {}, []

    def log(self, m):
        self.logs.append(m)

    def detail(self, _):
        pass

    def set(self, k, v):
        self.context[k] = v

    def wait_until(self, probe, *, timeout_s, interval_s=10, what=""):
        for _ in range(10):
            if r := probe():
                return r
        raise AssertionError(f"never: {what}")


def _seed_agent_runtime(rt_id="rt-local", runtime_id=None):
    from app.core.db import session_scope
    from app.models import Agent, AgentRuntime

    with session_scope() as s:
        s.add(
            Agent(
                id="ag-pv",
                name="pv",
                source="image",
                config={"region": "us-east-1"},
                image_uri="111.dkr.ecr.us-east-1.amazonaws.com/a:1",
                status="ready",
                checks={},
            )
        )
        s.add(
            AgentRuntime(
                id=rt_id,
                agent_id="ag-pv",
                region="us-east-1",
                network_mode="PUBLIC",
                runtime_id=runtime_id,
                status="queued",
            )
        )


def test_stage_deploy_v2_waits_for_busy_runtime_and_records_version(stub_aws, monkeypatch):
    from tests.conftest import StubClient

    monkeypatch.setattr(pa.proj, "require_region", lambda r: {"acr_role_arn": "arn:role"})
    _seed_agent_runtime(runtime_id="rt-1")
    statuses = iter(["UPDATING", "READY", "UPDATING", "READY"])
    ctl = StubClient(
        get_agent_runtime=lambda **_: {"status": next(statuses), "platformVersion": "V2"},
        update_agent_runtime={"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1"},
    )
    stub_aws({"bedrock-agentcore-control": ctl})
    pa.stage_deploy(_Ctx("rt-local", {"platform_version": "auto"}))
    names = [n for n, _ in ctl.calls]
    # busy runtime polled before the update, so no ConflictException
    assert names.index("update_agent_runtime") > names.index("get_agent_runtime")
    upd = next(kw for n, kw in ctl.calls if n == "update_agent_runtime")
    assert upd["platformVersion"] == "V2"
    assert pa.load_runtime("rt-local")["platform_version"] == "V2"


def test_stage_deploy_keeps_stored_version_without_request(stub_aws, monkeypatch):
    from tests.conftest import StubClient

    monkeypatch.setattr(pa.proj, "require_region", lambda r: {"acr_role_arn": "arn:role"})
    _seed_agent_runtime()
    pa.save_runtime("rt-local", platform_version="V1")
    ctl = StubClient(
        list_agent_runtimes={"agentRuntimes": []},
        create_agent_runtime={"agentRuntimeId": "rt-9", "agentRuntimeArn": "arn:rt-9"},
        get_agent_runtime={"status": "READY"},  # no platformVersion field → V1
    )
    stub_aws({"bedrock-agentcore-control": ctl})
    pa.stage_deploy(_Ctx("rt-local", {}))
    create = next(kw for n, kw in ctl.calls if n == "create_agent_runtime")
    assert create["platformVersion"] == "V1"
    assert pa.load_runtime("rt-local")["platform_version"] == "V1"


def test_deploy_endpoint_rejects_v2_in_unsupported_region(client):
    from app.core.db import session_scope
    from app.models import Agent

    with session_scope() as s:
        s.add(
            Agent(
                id="ag-x",
                name="x",
                source="image",
                config={"region": "sa-east-1"},
                status="ready",
                checks={},
            )
        )
    r = client.post("/api/agents/ag-x/runtimes", json={"platform_version": "V2"})
    assert r.status_code >= 400
    assert "agent.platform_unsupported_region" in r.text
    r = client.post("/api/agents/ag-x/runtimes", json={"platform_version": "V9"})
    assert r.status_code == 422


def test_init_db_adds_new_nullable_columns(tmp_path):
    import sqlalchemy as sa

    from app.core import db

    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("CREATE TABLE agent_runtimes (id VARCHAR(64) PRIMARY KEY)"))
    db.init_db(url)
    cols = {c["name"] for c in sa.inspect(sa.create_engine(url)).get_columns("agent_runtimes")}
    assert {"platform_version", "runtime_arn", "cluster_id"} <= cols
    assert "last_smoke" not in cols  # NOT NULL columns are never added in place


# ---------------- eval-only OTEL twin (trace recording) ----------------


def _seed_vpc_runtime(image="img:1"):
    from app.core.db import session_scope
    from app.models import Agent, AgentRuntime, Cluster

    with session_scope() as s:
        s.add(
            Cluster(
                id="cl-1",
                name="dev",
                region="us-east-1",
                source="create",
                status="ready",
                network={"private_subnets": ["subnet-a"], "sg_acr": "sg-acr"},
                components={},
                params={},
            )
        )
        s.add(
            Agent(
                id="ag-1", name="ob", source="template", config={}, image_uri=image, status="ready"
            )
        )
        s.flush()
        s.add(
            AgentRuntime(
                id="rt-1",
                agent_id="ag-1",
                cluster_id="cl-1",
                region="us-east-1",
                network_mode="VPC",
                runtime_id="tr-1",
                runtime_arn="arn:train",
                image_uri=image,
                platform_version="V2",
                status="ready",
            )
        )


def test_training_deploy_sends_no_environment(stub_aws):
    from tests.conftest import StubClient

    ctl = StubClient(
        list_agent_runtimes={"agentRuntimes": []},
        create_agent_runtime={"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1"},
    )
    stub_aws({"bedrock-agentcore-control": ctl})
    svc.deploy_runtime("us-east-1", "n", "img", "role", svc.network_config("PUBLIC"))
    (_, create) = next(c for c in ctl.calls if c[0] == "create_agent_runtime")
    assert "environmentVariables" not in create  # training runtimes: process unchanged


def test_obs_runtime_created_reused_and_updated_on_new_image(stub_aws, monkeypatch):
    from tests.conftest import StubClient

    monkeypatch.setattr(pa.proj, "require_region", lambda r: {"acr_role_arn": "arn:role"})
    _seed_vpc_runtime()
    ctl = StubClient(
        list_agent_runtimes={"agentRuntimes": []},
        create_agent_runtime={"agentRuntimeId": "ob-1", "agentRuntimeArn": "arn:obs"},
        update_agent_runtime={"agentRuntimeId": "ob-1", "agentRuntimeArn": "arn:obs"},
        get_agent_runtime={"status": "READY", "platformVersion": "V2"},
    )
    stub_aws({"bedrock-agentcore-control": ctl})
    assert pa.ensure_obs_runtime(_Ctx("ev-1", {}), "rt-1") == "arn:obs"
    (_, create) = next(c for c in ctl.calls if c[0] == "create_agent_runtime")
    assert create["agentRuntimeName"] == "tp_ob_dev_obs"
    env = create["environmentVariables"]
    assert env["TP_OBSERVABILITY"] == "1" and env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert env["AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT"] == "true"
    assert env["UNIFIED_TRACES_DESTINATION_ENABLED"] == "false"  # aws/spans, no role change
    assert create["platformVersion"] == "V2"  # same platform as the training twin
    assert create["networkConfiguration"]["networkModeConfig"]["securityGroups"] == ["sg-acr"]
    # training runtime row untouched
    assert pa.load_runtime("rt-1")["runtime_arn"] == "arn:train"

    n = len(ctl.calls)
    assert pa.ensure_obs_runtime(_Ctx("ev-2", {}), "rt-1") == "arn:obs"
    assert len(ctl.calls) == n  # READY on the same image: reused, no AWS call

    pa.save_runtime("rt-1", image_uri="img:2")  # agent rebuilt → twin is stale
    pa.ensure_obs_runtime(_Ctx("ev-3", {}), "rt-1")
    upd = next(kw for name, kw in ctl.calls[n:] if name == "update_agent_runtime")
    assert upd["agentRuntimeId"] == "ob-1" and upd["agentRuntimeArtifact"] == {
        "containerConfiguration": {"containerUri": "img:2"}
    }


def test_observed_eval_needs_transaction_search(client, stub_aws, monkeypatch):
    from tests.conftest import StubClient

    _seed_vpc_runtime()
    xray = StubClient(get_trace_segment_destination={"Destination": "XRay", "Status": "ACTIVE"})
    stub_aws({"xray": xray})
    body = {
        "name": "e",
        "endpoint_id": "ep-x",
        "agent_runtime_id": "rt-1",
        "dataset_id": "ds-x",
        "observe": True,
    }
    r = client.post("/api/evals", json=body)
    assert r.status_code == 409 and r.json()["code"] == "eval.observability_off"
    assert r.json()["detail"] == {"region": "us-east-1"}
    assert client.get("/api/evals").json() == []  # nothing created


def test_deleting_runtime_deletes_its_obs_twin(client, stub_aws):
    from tests.conftest import StubClient

    _seed_vpc_runtime()
    pa.save_obs("rt-1", runtime_id="ob-1", status="ready")
    ctl = StubClient(delete_agent_runtime={})
    stub_aws({"bedrock-agentcore-control": ctl})
    assert client.delete("/api/agents/ag-1/runtimes/rt-1").json() == {"ok": True}
    deleted = [kw["agentRuntimeId"] for name, kw in ctl.calls if name == "delete_agent_runtime"]
    assert deleted == ["tr-1", "ob-1"]
