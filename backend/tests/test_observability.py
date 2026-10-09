"""Transaction Search enablement and eval trace queries (services/observability.py)."""

import json

import pytest

from app.services import observability as obs
from tests.conftest import StubClient

pytest_plugins = ["tests.test_evals"]  # eval_env: an eval with results.jsonl + dataset


def _xray(state):
    return StubClient(
        get_trace_segment_destination=lambda: state,
        update_trace_segment_destination=lambda **kw: state.update(
            Destination=kw["Destination"], Status="PENDING"
        ),
    )


def test_status_mapping(stub_aws):
    for dest, status, want in (
        ("XRay", "ACTIVE", "off"),
        ("CloudWatchLogs", "PENDING", "pending"),
        ("CloudWatchLogs", "ACTIVE", "active"),
    ):
        stub_aws({"xray": _xray({"Destination": dest, "Status": status})})
        assert obs.transaction_search_status("us-east-1")["transaction_search"] == want


def test_enable_requires_confirmation_and_writes_nothing(client, stub_aws):
    svcs = stub_aws(
        {
            "xray": _xray({"Destination": "XRay", "Status": "ACTIVE"}),
            "logs": StubClient(put_resource_policy={}),
            "sts": StubClient(get_caller_identity={"Account": "123456789012"}),
        }
    )
    r = client.post("/api/observability/transaction-search", json={"region": "us-east-1"})
    assert r.status_code == 400 and r.json()["code"] == "observability.confirm_required"
    assert svcs["logs"].calls == [] and [c[0] for c in svcs["xray"].calls] == []
    assert client.get("/api/observability/status?region=not-a-region").status_code == 422


def test_enable_writes_scoped_policy_then_destination(client, stub_aws):
    state = {"Destination": "XRay", "Status": "ACTIVE"}
    svcs = stub_aws(
        {
            "xray": _xray(state),
            "logs": StubClient(put_resource_policy={}),
            "sts": StubClient(get_caller_identity={"Account": "123456789012"}),
        }
    )
    r = client.post(
        "/api/observability/transaction-search", json={"region": "us-east-1", "confirm": True}
    )
    assert r.json()["transaction_search"] == "pending"
    ((name, kw),) = svcs["logs"].calls
    assert name == "put_resource_policy" and kw["policyName"] == obs.RESOURCE_POLICY_NAME
    stmt = json.loads(kw["policyDocument"])["Statement"][0]
    assert stmt["Principal"] == {"Service": "xray.amazonaws.com"}
    assert stmt["Action"] == "logs:PutLogEvents"
    assert stmt["Resource"] == [
        "arn:aws:logs:us-east-1:123456789012:log-group:aws/spans:*",
        "arn:aws:logs:us-east-1:123456789012:log-group:/aws/application-signals/data:*",
    ]
    assert stmt["Condition"]["StringEquals"] == {"aws:SourceAccount": "123456789012"}
    assert stmt["Condition"]["ArnLike"] == {
        "aws:SourceArn": "arn:aws:xray:us-east-1:123456789012:*"
    }
    assert ("update_trace_segment_destination", {"Destination": "CloudWatchLogs"}) in svcs[
        "xray"
    ].calls
    # already on (pending or active): no second write
    client.post(
        "/api/observability/transaction-search", json={"region": "us-east-1", "confirm": True}
    )
    assert len(svcs["logs"].calls) == 1


# ---------------- span tree (Strands 1.18 tracer shape) ----------------

T0 = 1_791_500_000_000_000_000  # ns
MS = 1_000_000
KEY = "tp-" + "B" * 30
SID = "1f24e6c4-6de0-45e2-9ff6-28a3cfb7df01"


def _span(sid, parent, name, start_ms, end_ms, attrs=None, events=None, status="UNSET"):
    rec = {
        "traceId": "t" * 32,
        "spanId": sid,
        "name": name,
        "kind": "INTERNAL",
        "startTimeUnixNano": str(T0 + start_ms * MS),
        "endTimeUnixNano": str(T0 + end_ms * MS),
        "attributes": {"session.id": SID, **(attrs or {})},
        "status": {"code": status},
        "events": events or [],
    }
    if parent:
        rec["parentSpanId"] = parent
    return rec


STRANDS_SPANS = [
    _span("a", None, "invoke_agent Strands Agents", 0, 10_000,
          {"gen_ai.operation.name": "invoke_agent", "gen_ai.usage.input_tokens": 999,
           "gen_ai.tool.definitions": "[...]"}),
    _span("c", "a", "execute_event_loop_cycle", 10, 9_990),
    _span("l1", "c", "chat", 20, 4_020,
          {"gen_ai.operation.name": "chat", "gen_ai.request.model": "qwen",
           "gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 80},
          [{"name": "gen_ai.user.message",
            "attributes": {"content": json.dumps([{"text": f"delete Alice, key {KEY}"}])}},
           {"name": "gen_ai.choice",
            "attributes": {"finish_reason": "tool_use", "message": json.dumps(
                [{"toolUse": {"toolUseId": "t1", "name": "shell", "input": {"cmd": "ls"}}}])}}]),
    _span("x1", "c", "execute_tool shell", 4_100, 4_600,
          {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "shell",
           "gen_ai.tool.call.id": "t1", "gen_ai.tool.status": "error"},
          [{"name": "gen_ai.tool.message",
            "attributes": {"role": "tool", "content": json.dumps({"cmd": "ls"}), "id": "t1"}}],
          status="ERROR"),
    _span("l2", "c", "chat", 4_700, 9_900,
          {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 1500,
           "gen_ai.usage.output_tokens": 20}),
    _span("orphan", "missing-parent", "chat", 9_950, 10_000, {"gen_ai.operation.name": "chat"}),
]  # fmt: skip


def test_build_trace_tree_offsets_categories_tokens():
    tr = obs.build_trace(list(reversed(STRANDS_SPANS)) + [STRANDS_SPANS[0]])  # order, dupes
    ids = [(s["span_id"], s["depth"]) for s in tr["spans"]]
    assert ids == [("a", 0), ("c", 1), ("l1", 2), ("x1", 2), ("l2", 2), ("orphan", 0)]
    by = {s["span_id"]: s for s in tr["spans"]}
    assert [by[k]["category"] for k in ("a", "c", "l1", "x1")] == ["agent", "agent", "llm", "tool"]
    assert (
        tr["duration_ms"] == 10_000 and by["a"]["offset_pct"] == 0 and by["a"]["width_pct"] == 100
    )
    assert by["x1"]["start_ms"] == 4_100 and by["x1"]["duration_ms"] == 500
    assert by["x1"]["offset_pct"] == 41 and by["x1"]["width_pct"] == 5
    assert by["x1"]["status"] == "error" and by["x1"]["tool"] == "shell"
    assert by["l1"]["model"] == "qwen" and by["l1"]["tokens"] == {"input": 1200, "output": 80}
    totals = tr["totals"]  # agent aggregate (999) is not double counted
    assert (totals["llm_calls"], totals["tool_calls"], totals["errors"]) == (3, 1, 1)
    assert (totals["input_tokens"], totals["output_tokens"]) == (2700, 100)
    assert "gen_ai.tool.definitions" not in by["a"]["attributes"]


def test_span_messages_from_events_are_normalized_and_redacted():
    tr = obs.build_trace(STRANDS_SPANS)
    by = {s["span_id"]: s for s in tr["spans"]}
    (user,) = by["l1"]["input_messages"]
    assert user["role"] == "user" and user["blocks"][0]["text"] == "delete Alice, key ***"
    (out,) = by["l1"]["output_messages"]
    assert out["blocks"][0]["type"] == "tool_use" and out["blocks"][0]["name"] == "shell"
    (tool_in,) = by["x1"]["input_messages"]
    assert tool_in["blocks"][0]["type"] == "tool_use" and json.loads(
        tool_in["blocks"][0]["text"]
    ) == {"cmd": "ls"}
    assert KEY not in json.dumps(tr)


def test_latest_conventions_parts_and_cycles():
    parts = json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": "hi"}]},
         {"role": "assistant", "parts": [{"type": "tool_call", "name": "f", "arguments": {"a": 1}}]}]
    )  # fmt: skip
    recs = [
        _span("p", "q", "chat", 0, 10, {"gen_ai.input.messages": parts}),
        _span("q", "p", "execute_tool f", 1, 5, {"gen_ai.tool.name": "f"}),  # p <-> q cycle
    ]
    tr = obs.build_trace(recs)
    assert sorted(s["span_id"] for s in tr["spans"]) == ["p", "q"]  # both kept, once each
    p = next(s for s in tr["spans"] if s["span_id"] == "p")
    assert [b["type"] for m in p["input_messages"] for b in m["blocks"]] == ["text", "tool_use"]
    assert obs.build_trace([{"spanId": "x"}]) is None  # no timestamps: nothing to draw


def _observed_eval(ended_minutes_ago):
    from datetime import UTC, datetime, timedelta

    from app.core.db import session_scope
    from app.models import Eval
    from app.services import evals as esvc
    from tests.test_evals import ITEMS

    with session_scope() as s:
        e = s.get(Eval, "ev-1")
        e.observe = True
        e.started_at = datetime.now(UTC) - timedelta(minutes=ended_minutes_ago + 5)
        e.ended_at = datetime.now(UTC) - timedelta(minutes=ended_minutes_ago)
    esvc.results_path("ev-1").write_text("\n".join(json.dumps(it) for it in ITEMS))


def _logs(records, fail=None):
    def start_query(**kw):
        if fail:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": fail, "Message": "x"}}, "StartQuery")
        return {"queryId": "q-1"}

    rows = [
        [{"field": "@message", "value": json.dumps(r)}, {"field": "@ptr", "value": "p"}]
        for r in records
    ]
    return StubClient(
        start_query=start_query, get_query_results={"status": "Complete", "results": rows}
    )


@pytest.fixture
def observed(eval_env, monkeypatch):
    monkeypatch.setattr(obs, "_cache", {})
    _observed_eval(ended_minutes_ago=60)


def test_trace_ready_queries_by_session_id(client, stub_aws, observed):
    logs = stub_aws({"logs": _logs(STRANDS_SPANS)})["logs"]
    d = client.get("/api/evals/ev-1/samples/0/trace").json()
    assert d["state"] == "ready" and d["session_id"] == SID
    assert d["trace"]["totals"]["llm_calls"] == 3
    (_, kw) = logs.calls[0]
    assert f'attributes.session.id = "{SID}"' in kw["queryString"]
    assert kw["queryString"].startswith(obs.SPANS_SOURCE) and kw["startTime"] < kw["endTime"]
    client.get("/api/evals/ev-1/samples/0/trace")
    assert len([c for c in logs.calls if c[0] == "start_query"]) == 1  # cached
    client.get("/api/evals/ev-1/samples/0/trace?force=true")
    assert len([c for c in logs.calls if c[0] == "start_query"]) == 2


def test_trace_empty_pending_and_errors(client, stub_aws, observed, monkeypatch):
    stub_aws({"logs": _logs([])})
    assert client.get("/api/evals/ev-1/samples/0/trace").json() == {
        "state": "empty", "session_id": SID, "reason": "no_spans",
    }  # fmt: skip
    # invocation failed → no runtime session to look up
    assert client.get("/api/evals/ev-1/samples/4/trace").json()["reason"] == "no_session"
    monkeypatch.setattr(obs, "_cache", {})
    _observed_eval(ended_minutes_ago=2)  # spans may still be ingesting
    assert client.get("/api/evals/ev-1/samples/0/trace").json()["state"] == "pending"
    monkeypatch.setattr(obs, "_cache", {})
    stub_aws({"logs": _logs([], fail="AccessDeniedException")})
    r = client.get("/api/evals/ev-1/samples/0/trace")
    assert r.status_code == 502 and r.json()["code"] == "observability.query_failed"


def test_trace_requires_observed_eval(client, eval_env):
    r = client.get("/api/evals/ev-1/samples/0/trace")
    assert r.status_code == 404 and r.json()["code"] == "eval.trace_unavailable"


def test_real_officebench_session_from_aws_spans():
    """One real OfficeBench rollout on the eval-only runtime (2026-10-09, us-east-1,
    ADOT 0.21 + Strands 1.18, AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true), as returned by
    query_session_spans; account id masked, long strings clipped."""
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "strands_officebench_spans.json"
    tr = obs.build_trace(json.loads(path.read_text()))
    names = [(s["depth"], s["name"], s["category"]) for s in tr["spans"]]
    assert names[:3] == [
        (0, "POST /invocations", "other"),
        (1, "S3.GetObject", "other"),
        (1, "invoke_agent Strands Agents", "agent"),
    ]
    assert (4, "POST", "other") in names  # the model HTTP call nests under its chat span
    t = tr["totals"]
    # LLM spans add up to the agent span's own aggregate (4044 in / 295 out)
    assert (t["llm_calls"], t["tool_calls"], t["input_tokens"], t["output_tokens"]) == (
        2,
        1,
        4044,
        295,
    )
    tool = next(s for s in tr["spans"] if s["category"] == "tool")
    assert tool["name"] == "execute_tool calendar_create_event"
    assert tool["input_messages"][0]["blocks"][0]["type"] == "tool_use"
    out = tool["output_messages"][0]
    assert out["role"] == "tool" and out["blocks"][0]["type"] == "tool_result"
    assert "Successfully create" in out["blocks"][0]["text"]
    cycle = next(s for s in tr["spans"] if s["name"] == "execute_event_loop_cycle")
    # the cycle's choice carries the tool results it fed back (`tool.result`)
    assert any(b["type"] == "tool_result" for m in cycle["output_messages"] for b in m["blocks"])
    assert "434444145045" not in json.dumps(tr)


def test_exception_events_mark_the_span_failed():
    rec = _span(
        "e", None, "invoke_agent Strands Agents", 0, 10,
        events=[{"name": "exception", "attributes": {
            "exception.type": "strands.types.exceptions.MaxTokensReachedException",
            "exception.message": f"limit reached, key {KEY}",
            "exception.stacktrace": "Traceback ...",
        }}],
    )  # fmt: skip
    (s,) = obs.build_trace([rec])["spans"]
    assert s["status"] == "error" and s["exceptions"][0]["type"].endswith(
        "MaxTokensReachedException"
    )
    assert KEY not in s["exceptions"][0]["message"]
