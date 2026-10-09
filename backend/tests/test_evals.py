"""Eval sample views: state, transcript, task input and redaction (services/evals.py)."""

import json

import pandas as pd
import pytest

from app.core.db import session_scope
from app.models import Agent, AgentRuntime, Cluster, Dataset, Eval, InferenceEndpoint
from app.services import evals as esvc
from app.services import serving as svc

SID = "1f24e6c4-6de0-45e2-9ff6-28a3cfb7df01"
KEY = "tp-" + "A" * 32  # an inference endpoint API key (services.serving.api_key format)


def _ok(i, reward=1.0, **extra):
    return {
        "index": i,
        "success": True,
        "elapsed": 43.2,
        "error": None,
        "result": {
            "rewards": reward,
            "status_code": 200,
            "stop_reason": "end_turn",
            "result_key": f"evals/ev-1/in-{i}/{SID}.json",
            "s3_bucket": "b",
            "input_id": f"in-{i}",
            "messages": [
                {"role": "user", "content": [{"text": "delete student Alice"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"text": "Reading the file."},
                        {
                            "toolUse": {
                                "toolUseId": "t1",
                                "name": "excel_read_file",
                                "input": {"p": 1},
                            }
                        },
                        {"toolUse": {"toolUseId": "t2", "name": "shell", "input": {"cmd": "ls"}}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": "t1",
                                "status": "success",
                                "content": [{"text": "A|90"}],
                            }
                        },
                        {
                            "toolResult": {
                                "toolUseId": "t2",
                                "status": "error",
                                "content": [{"json": {"e": 1}}],
                            }
                        },
                    ],
                },
                {"role": "assistant", "content": [{"text": "Done."}]},
            ],
            **extra,
        },
    }


ITEMS = [
    _ok(0),
    _ok(1, reward=[0, 0.0]),
    {
        "index": 2,
        "success": True,
        "result": {
            "status_code": 500,
            "stop_reason": "list index out of range",
            "traceback": "Traceback ...\nIndexError: x",
        },
    },
    {"index": 3, "success": True, "result": {"rewards": 0.0, "stop_reason": "max_tokens"}},
    {"index": 4, "success": False, "error": "timeout after 1800s", "result": None},
]


def test_states_agree_with_eval_summary():
    states = [esvc.sample_state(it) for it in ITEMS]
    assert states == ["scored", "scored", "acr_failed", "truncated", "invoke_failed"]
    s = svc.summarize_eval(ITEMS)
    # summary: scored includes truncation (as 0); failed = both failure kinds
    assert s["scored"] == states.count("scored") + states.count("truncated")
    assert s["failed"] == states.count("acr_failed") + states.count("invoke_failed")
    assert s["truncated"] == states.count("truncated")


def test_session_id_from_result_key_or_stored_field():
    assert esvc.session_id_of(ITEMS[0]) == SID
    assert esvc.session_id_of({"session_id": SID.upper(), "result": {}}) == SID
    assert esvc.session_id_of(ITEMS[4]) is None  # invocation failed: no artifact, no session
    assert esvc.session_id_of({"result": {"result_key": "evals/x/y/not-a-uuid.json"}}) is None


def test_redaction_by_key_and_by_value():
    out = esvc.redact(
        {
            "api_key": "anything",
            "nested": [{"Authorization": "x"}, f"use {KEY} now", "AKIAABCDEFGHIJKLMNOP", "ok"],
            "text": "Bearer abcdefghijklmnopqrstuvwxyz",
        }
    )
    assert out["api_key"] == "***" and out["nested"][0]["Authorization"] == "***"
    assert out["nested"][1] == "use *** now" and out["nested"][2] == "***"
    assert out["nested"][3] == "ok" and out["text"] == "***"


def test_normalize_messages_blocks_and_clipping():
    msgs, more = esvc.normalize_messages(ITEMS[0]["result"]["messages"])
    assert not more and [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    tu = msgs[1]["blocks"][1]
    assert (
        tu["type"] == "tool_use"
        and tu["name"] == "excel_read_file"
        and json.loads(tu["text"]) == {"p": 1}
    )
    tr = msgs[2]["blocks"]
    assert tr[0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "status": "success",
        "text": "A|90",
    }
    assert tr[1]["status"] == "error" and json.loads(tr[1]["text"]) == {"e": 1}
    long, _ = esvc.normalize_messages([{"role": "assistant", "content": "x" * (esvc.TEXT_MAX + 5)}])
    assert long[0]["blocks"][0]["truncated"] and len(long[0]["blocks"][0]["text"]) == esvc.TEXT_MAX
    many, more = esvc.normalize_messages([{"role": "user", "content": "a"}] * (esvc.MSG_MAX + 1))
    assert more and len(many) == esvc.MSG_MAX
    reasoning, _ = esvc.normalize_messages(
        [
            {
                "role": "assistant",
                "content": [{"reasoningContent": {"reasoningText": {"text": "hmm"}}}],
            }
        ]
    )
    assert reasoning[0]["blocks"][0] == {"type": "reasoning", "text": "hmm"}


@pytest.fixture
def eval_env(tmp_path):
    """An eval with results.jsonl and a dataset split cached locally (no S3)."""
    with session_scope() as s:
        s.add(
            Cluster(
                id="cl-1",
                name="dev",
                region="us-east-1",
                source="create",
                status="ready",
                network={},
                components={},
                params={},
            )
        )
        s.add(
            Agent(
                id="ag-1",
                name="ob",
                source="template",
                template_id="officebench",
                config={},
                contract="verl",
            )
        )
        s.flush()
        s.add(
            AgentRuntime(
                id="rt-1", agent_id="ag-1", cluster_id="cl-1", region="us-east-1", status="ready"
            )
        )
        s.add(
            InferenceEndpoint(
                id="ep-1",
                name="ep",
                cluster_id="cl-1",
                model_source={},
                served_model_name="m",
                instance_group="g",
                status="ready",
            )
        )
        s.add(
            Dataset(
                id="ds-1",
                name="ob",
                region="us-east-1",
                source="builtin:officebench",
                status="ready",
                splits={"val": {"s3_key": "datasets/ds-1/val.parquet", "rows": 5}},
                sample=[],
                stats={},
            )
        )
        s.flush()
        s.add(
            Eval(
                id="ev-1",
                name="e",
                endpoint_id="ep-1",
                agent_runtime_id="rt-1",
                dataset_id="ds-1",
                split="val",
                limit=5,
                status="succeeded",
                summary={},
            )
        )
    lines = [json.dumps(it) for it in ITEMS]
    lines.insert(2, "{half-written")  # tolerated
    esvc.results_path("ev-1").write_text("\n".join(lines))
    from app.services import datasets as dsvc

    df = pd.DataFrame(
        {
            "payload": [{"task_uri": "s3://b/t.json", "api_key": KEY, "n": 1}] * 5,
            "prompt": [
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "delete Alice"}]
            ]
            * 5,
        }
    )
    df.to_parquet(dsvc.local_dir("ds-1") / "val.parquet")


def test_samples_api_lists_filters_and_pages(client, eval_env):
    d = client.get("/api/evals/ev-1/samples").json()
    assert d["total"] == 5 and d["observe"] is False
    assert d["counts"] == {"scored": 2, "truncated": 1, "acr_failed": 1, "invoke_failed": 1}
    first = d["samples"][0]
    assert (first["turns"], first["tool_calls"], first["session_id"]) == (2, 2, SID)
    failed = client.get("/api/evals/ev-1/samples?status=failed").json()
    assert [s["index"] for s in failed["samples"]] == [2, 4]
    page = client.get("/api/evals/ev-1/samples?offset=3&limit=1").json()
    assert [s["index"] for s in page["samples"]] == [3]
    assert client.get("/api/evals/ev-1/samples?status=bogus").status_code == 422
    assert client.get("/api/evals/nope/samples").json()["code"] == "eval.not_found"


def test_sample_detail_is_redacted_and_complete(client, eval_env):
    r = client.get("/api/evals/ev-1/samples/0")
    raw = r.text
    d = r.json()
    assert d["input"]["prompt"] == "delete Alice"  # user turns only
    assert d["input"]["fields"] == {"task_uri": "s3://b/t.json", "n": "1"}  # api_key dropped
    assert len(d["messages"]) == 4 and d["traceback"] is None
    for leak in ("result_key", "s3_bucket", "input_id", "_rollout", KEY):
        assert leak not in raw, leak
    bad = client.get("/api/evals/ev-1/samples/2").json()
    assert (
        bad["state"] == "acr_failed" and bad["messages"] == [] and "IndexError" in bad["traceback"]
    )
    assert client.get("/api/evals/ev-1/samples/9").json()["code"] == "eval.sample_not_found"
