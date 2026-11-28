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
    assert d["counts"] == {
        "scored": 2,
        "truncated": 1,
        "acr_failed": 1,
        "invoke_failed": 1,
        "unscored": 0,
    }
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


# ---------------- per-Harness evaluation matrix (#11) ----------------

H = ["claude_code", "opencode"]


def _cell(i, task, harness, reward, tool_calls, stop="completed"):
    return {
        "index": i,
        "task_index": task,
        "harness": harness,
        "success": True,
        "result": {
            "reward": reward,
            "stop_reason": stop,
            "metrics": {"tool_calls": tool_calls, "correct": int(bool(reward and reward >= 1))},
        },
    }


def test_cells_expand_tasks_by_harness():
    payloads = [{"task_id": "a"}, {"task_id": "b", "harness": "mini_swe_agent"}]
    cells = esvc.eval_cells(payloads, H)
    assert [(c["task_index"], c["payload"]["harness"]) for c in cells] == [
        (0, "claude_code"),
        (0, "opencode"),
        (1, "claude_code"),  # the eval's Harnesses win over a dataset's own column
        (1, "opencode"),
    ]
    plain = esvc.eval_cells(payloads[:1], None)
    assert plain == [{"task_index": 0, "harness": None, "payload": {"task_id": "a"}}]


def test_a_cell_without_a_verifier_result_is_unscored_not_wrong():
    item = _cell(0, 0, "opencode", None, 3, stop="verifier_no_result")
    assert esvc.sample_state(item) == "unscored"
    s = svc.summarize_eval([item, _cell(1, 1, "opencode", 1.05, 2)])
    assert (s["unscored"], s["scored"], s["mean_reward_scored"]) == (1, 1, 1.05)


def test_summary_reports_pass_at_1_and_tool_calls_per_harness():
    items = [
        _cell(0, 0, "claude_code", 1.09, 4),
        _cell(1, 0, "opencode", 0.0, 9),
        _cell(2, 1, "claude_code", 0.0, 6),
        _cell(3, 1, "opencode", None, 2, stop="verifier_no_result"),
        {"index": 4, "task_index": 2, "harness": "opencode", "success": False, "error": "x"},
    ]
    by = svc.summarize_eval(items)["by_harness"]
    assert by["claude_code"] == {
        "n": 2,
        "scored": 2,
        "unscored": 0,
        "failed": 0,
        "graded": 2,
        "solved": 1,
        "pass_at_1": 0.5,
        "mean_tool_calls": 5.0,
    }
    # cells never graded (unscored, or the invocation failed) leave the denominator
    assert by["opencode"]["pass_at_1"] == 0.0 and by["opencode"]["n"] == 3
    assert by["opencode"]["unscored"] == 1 and by["opencode"]["failed"] == 1
    assert by["opencode"]["graded"] == 1


def test_a_single_harness_summary_matches_the_plain_eval():
    plain = [dict(_cell(i, i, None, r, 2), harness=None) for i, r in enumerate([1.0, 0.0, 1.0])]
    one = [_cell(i, i, "claude_code", r, 2) for i, r in enumerate([1.0, 0.0, 1.0])]
    a, b = svc.summarize_eval(plain), svc.summarize_eval(one)
    assert {k: v for k, v in b.items() if k != "by_harness"} == a
    assert "by_harness" not in a


def test_compare_two_evals_per_harness():
    base = [
        _cell(0, 0, "claude_code", 1.0, 10),
        _cell(1, 1, "claude_code", 0.0, 8),
        _cell(2, 0, "opencode", 1.0, 6),
    ]
    trained = [
        _cell(0, 0, "claude_code", 1.08, 5),
        _cell(1, 1, "claude_code", 1.05, 7),
        _cell(2, 0, "opencode", 0.0, 3),
    ]
    out = esvc.compare(trained, base)
    cc = out["claude_code"]
    assert (cc["pass_at_1"], cc["other_pass_at_1"]) == (1.0, 0.5)
    # tool calls only on Tasks both solved: task 0 → 5 vs 10
    assert (cc["both_solved"], cc["mean_tool_calls"], cc["other_mean_tool_calls"]) == (1, 5.0, 10.0)
    assert cc["tool_call_change"] == -0.5
    assert out["opencode"]["both_solved"] == 0 and out["opencode"]["tool_call_change"] is None


def test_sample_rows_show_the_harness_and_input_comes_from_the_task(client, eval_env):
    cells = [_cell(0, 3, "opencode", 1.0, 2)]
    esvc.results_path("ev-1").write_text("\n".join(json.dumps(c) for c in cells))
    row = client.get("/api/evals/ev-1/samples").json()["samples"][0]
    assert (row["harness"], row["task_index"], row["tool_calls"]) == ("opencode", 3, 2)
    assert client.get("/api/evals/ev-1/samples/0").json()["input"]["prompt"] == "delete Alice"


def _harness_agent():
    with session_scope() as s:
        a = s.get(Agent, "ag-1")
        a.template_id = "multi_harness"
        a.config = {"params": {"harnesses": H}}
        a.contract = "verl-v2"
        s.get(AgentRuntime, "rt-1").contract = "verl-v2"


def _eval_body(**extra):
    return {
        "name": "m",
        "endpoint_id": "ep-1",
        "agent_runtime_id": "rt-1",
        "dataset_id": "ds-1",
        "split": "val",
        "limit": 5,
        **extra,
    }


def test_an_eval_runs_every_installed_harness_by_default(client, eval_env, monkeypatch):
    from app.jobs import engine as eng

    monkeypatch.setattr(eng.get_engine()._pool, "submit", lambda *a, **k: None)
    _harness_agent()
    r = client.post("/api/evals", json=_eval_body())
    assert r.status_code == 200, r.text
    with session_scope() as s:
        assert s.get(Eval, r.json()["id"]).harnesses == H
    picked = client.post("/api/evals", json=_eval_body(harnesses=["opencode"])).json()
    with session_scope() as s:
        assert s.get(Eval, picked["id"]).harnesses == ["opencode"]
    bad = client.post("/api/evals", json=_eval_body(harnesses=["codex"])).json()
    assert bad["code"] == "harness.not_installed"


def test_compare_endpoint_needs_the_same_tasks(client, eval_env):
    cells = [_cell(0, 0, "opencode", 1.0, 2)]
    esvc.results_path("ev-1").write_text(json.dumps(cells[0]))
    with session_scope() as s:
        s.add(
            Eval(
                id="ev-2",
                name="b",
                endpoint_id="ep-1",
                agent_runtime_id="rt-1",
                dataset_id="ds-1",
                split="val",
                limit=5,
                status="succeeded",
                summary={},
            )
        )
        s.add(
            Eval(
                id="ev-3",
                name="c",
                endpoint_id="ep-1",
                agent_runtime_id="rt-1",
                dataset_id="ds-1",
                split="test",
                limit=5,
                status="succeeded",
                summary={},
            )
        )
    esvc.results_path("ev-2").write_text(json.dumps(_cell(0, 0, "opencode", 1.0, 4)))
    d = client.get("/api/evals/ev-1/compare/ev-2").json()
    assert d["by_harness"]["opencode"]["tool_call_change"] == -0.5
    assert client.get("/api/evals/ev-1/compare/ev-3").json()["code"] == "eval.not_comparable"


def test_compare_uses_only_cells_both_evals_ran():
    """Different limits or Harness sets must not compare different Tasks."""
    mine = [_cell(0, 0, "opencode", 1.0, 2), _cell(1, 1, "opencode", 1.0, 2)]
    other = [_cell(0, 0, "opencode", 0.0, 2)]  # the other eval ran Task 0 only
    oc = esvc.compare(mine, other)["opencode"]
    assert (oc["matched"], oc["pass_at_1"], oc["other_pass_at_1"]) == (1, 1.0, 0.0)


def test_tool_calls_fall_back_to_the_transcript():
    item = dict(ITEMS[0])  # no metrics.tool_calls; two toolUse blocks in its messages
    assert esvc.tool_calls_of(item) == 2


def test_samples_can_be_filtered_to_unscored(client, eval_env):
    cells = [
        _cell(0, 0, "opencode", 1.0, 2),
        _cell(1, 1, "opencode", None, 1, stop="verifier_no_result"),
    ]
    esvc.results_path("ev-1").write_text("\n".join(json.dumps(c) for c in cells))
    d = client.get("/api/evals/ev-1/samples?status=unscored").json()
    assert [r["index"] for r in d["samples"]] == [1]
