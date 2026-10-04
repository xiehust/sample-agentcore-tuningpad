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
