"""AgentCore observability for evals: CloudWatch Transaction Search and span queries.

Transaction Search routes X-Ray spans into CloudWatch Logs (`aws/spans`), where the eval
trace view queries them by `session.id`. It is an account-level setting per Region, so it
is only ever turned on after the operator confirms it in the console.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

from ..core import aws
from ..core.errors import AppError

SPANS_LOG_GROUP = "aws/spans"
APP_SIGNALS_LOG_GROUP = "/aws/application-signals/data"
RESOURCE_POLICY_NAME = "TuningPadTransactionSearch"
# split delivery lands in aws/spans; unified delivery in the runtime's own log group
SPANS_SOURCE = "SOURCE logGroups(namePrefix: ['aws/spans', '/aws/bedrock-agentcore/runtimes/'])"
SPANS_PER_TRACE = 500
QUERY_DEADLINE_S = 55
CACHE_TTL_S = 60
PENDING_WINDOW = timedelta(minutes=15)  # spans may still be ingesting this long after an eval
TEXT_MAX = 20_000
ATTR_TEXT_MAX = 2_000


# ---------------- Transaction Search ----------------


def transaction_search_status(region: str) -> dict[str, Any]:
    """{region, transaction_search: active | pending | off, destination}."""
    d = aws.client("xray", region).get_trace_segment_destination()
    dest, status = d.get("Destination"), str(d.get("Status") or "").upper()
    if dest != "CloudWatchLogs":
        state = "off"
    else:
        state = "active" if status == "ACTIVE" else "pending"
    return {"region": region, "transaction_search": state, "destination": dest}


def _spans_policy(region: str, account: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "TransactionSearchXRayAccess",
                    "Effect": "Allow",
                    "Principal": {"Service": "xray.amazonaws.com"},
                    "Action": "logs:PutLogEvents",
                    "Resource": [
                        f"arn:aws:logs:{region}:{account}:log-group:{SPANS_LOG_GROUP}:*",
                        f"arn:aws:logs:{region}:{account}:log-group:{APP_SIGNALS_LOG_GROUP}:*",
                    ],
                    "Condition": {
                        "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{region}:{account}:*"},
                        "StringEquals": {"aws:SourceAccount": account},
                    },
                }
            ],
        }
    )


def enable_transaction_search(region: str, confirm: bool) -> dict[str, Any]:
    """Allow X-Ray to write `aws/spans`, then send trace segments to CloudWatch Logs.
    Idempotent; refuses without an explicit confirmation (account-level change)."""
    if not confirm:
        raise AppError(
            "observability.confirm_required",
            "enabling CloudWatch Transaction Search changes an account-level X-Ray setting; "
            "confirm it first",
            detail={"region": region},
        )
    current = transaction_search_status(region)
    if current["transaction_search"] != "off":
        return current
    account = aws.client("sts", region).get_caller_identity()["Account"]
    aws.client("logs", region).put_resource_policy(
        policyName=RESOURCE_POLICY_NAME, policyDocument=_spans_policy(region, account)
    )
    aws.client("xray", region).update_trace_segment_destination(Destination="CloudWatchLogs")
    return transaction_search_status(region)


# ---------------- span query ----------------


def _epoch(dt: datetime) -> int:
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())


def query_session_spans(
    region: str, session_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Raw span records (OTLP JSON, one per @message) of one AgentCore runtime session.
    The session id is validated as a UUID before it is put into the query string."""
    sid = str(uuid.UUID(session_id))
    query = (
        f"{SPANS_SOURCE}\n"
        f'| filter attributes.session.id = "{sid}" and ispresent(startTimeUnixNano)\n'
        f"| fields @message\n| limit {SPANS_PER_TRACE}"
    )
    logs = aws.client("logs", region)
    qid = None
    for attempt in (0, 1):
        try:
            qid = logs.start_query(startTime=_epoch(start), endTime=_epoch(end), queryString=query)[
                "queryId"
            ]
            break
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                return []  # no span log group yet: nothing was ever recorded here
            if attempt == 0 and code in ("ThrottlingException", "LimitExceededException"):
                time.sleep(1.5)
                continue
            raise AppError(
                "observability.query_failed",
                f"CloudWatch Logs Insights query failed: {code}",
                status=502,
            ) from e
    deadline = time.time() + QUERY_DEADLINE_S
    while True:
        try:
            res = logs.get_query_results(queryId=qid)
        except ClientError as e:
            raise AppError(
                "observability.query_failed",
                f"CloudWatch Logs Insights query failed: {e.response['Error']['Code']}",
                status=502,
            ) from e
        if res["status"] == "Complete":
            break
        if res["status"] in ("Failed", "Cancelled", "Timeout") or time.time() > deadline:
            raise AppError(
                "observability.query_failed",
                f"CloudWatch Logs Insights query {res['status'].lower()}",
                status=502,
            )
        time.sleep(0.8)
    out = []
    for row in res.get("results") or []:
        msg = next((f["value"] for f in row if f.get("field") == "@message"), None)
        try:
            rec = json.loads(msg) if msg else None
        except ValueError:
            rec = None
        if isinstance(rec, dict):
            out.append(rec)
    return out


# ---------------- span normalization (Strands gen_ai conventions) ----------------

# message events of Strands' tracer (pre-1.37 GenAI conventions); the latest conventions put
# the same content in gen_ai.input.messages / gen_ai.output.messages ("parts" format)
_INPUT_EVENTS = {
    "gen_ai.system.message": "system",
    "gen_ai.user.message": "user",
    "gen_ai.assistant.message": "assistant",
    "gen_ai.tool.message": "tool",
}
_HEAVY_ATTRS = {"gen_ai.tool.definitions", "gen_ai.agent.tools", "gen_ai.input.messages",
                "gen_ai.output.messages"}  # fmt: skip


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def category(name: str, attrs: dict[str, Any]) -> str:
    op = str(attrs.get("gen_ai.operation.name") or "")
    if op == "execute_tool" or name.startswith("execute_tool") or attrs.get("gen_ai.tool.name"):
        return "tool"
    if op in ("chat", "text_completion", "generate_content") or name == "chat":
        return "llm"
    if op in ("invoke_agent", "create_agent") or name.startswith(
        ("invoke_agent", "execute_event_loop_cycle")
    ):
        return "agent"
    return "other"


def _parts_messages(raw: Any) -> list[dict[str, Any]]:
    """Latest GenAI conventions: [{role, parts: [{type, content|name|arguments|response}]}]."""
    from .evals import redact

    out = []
    for m in _json(raw) if isinstance(_json(raw), list) else []:
        if not isinstance(m, dict):
            continue
        blocks = []
        for p in m.get("parts") or []:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "tool_call":
                body = p.get("arguments")
                blocks.append({"type": "tool_use", "name": str(p.get("name") or ""), "text": body})
            elif t == "tool_call_response":
                blocks.append({"type": "tool_result", "text": p.get("response")})
            elif t == "reasoning":
                blocks.append({"type": "reasoning", "text": p.get("content")})
            else:
                blocks.append({"type": "text", "text": p.get("content", p)})
        for b in blocks:
            b["text"] = redact(_clip_text(b["text"]))
        out.append({"role": str(m.get("role") or "unknown"), "blocks": blocks})
    return out


def _clip_text(v: Any, limit: int = TEXT_MAX) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2, default=str)
    return s if len(s) <= limit else s[:limit] + " …"


def span_messages(rec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(input, output) messages of a span from its events and gen_ai.*.messages attributes,
    in the transcript block format (see services.evals.normalize_messages)."""
    from .evals import normalize_messages

    attrs = rec.get("attributes") or {}
    is_tool = category(str(rec.get("name") or ""), attrs) == "tool"
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    for ev in rec.get("events") or []:
        name, ea = ev.get("name"), ev.get("attributes") or {}
        if name in _INPUT_EVENTS:
            content = _json(ea.get("content"))
            if name == "gen_ai.tool.message" and not isinstance(content, list):
                # execute_tool span: the call's arguments
                content = [{"toolUse": {"name": attrs.get("gen_ai.tool.name"), "input": content}}]
            msgs, _ = normalize_messages([{"role": _INPUT_EVENTS[name], "content": content}])
            inputs += msgs
        elif name == "gen_ai.choice":
            content = _json(ea.get("message"))
            if is_tool:  # execute_tool span: the tool's return value
                result = content if isinstance(content, list) else [{"text": content}]
                content = [
                    {"toolResult": {"status": attrs.get("gen_ai.tool.status"), "content": result}}
                ]
            msgs, _ = normalize_messages(
                [{"role": "tool" if is_tool else "assistant", "content": content}]
            )
            outputs += msgs
            if ea.get("tool.result"):  # event-loop cycle: the tool results it fed back
                tr, _ = normalize_messages([{"role": "tool", "content": _json(ea["tool.result"])}])
                outputs += tr
        elif name == "gen_ai.client.inference.operation.details":
            inputs += _parts_messages(ea.get("gen_ai.input.messages"))
            outputs += _parts_messages(ea.get("gen_ai.output.messages"))
    if not inputs and attrs.get("gen_ai.input.messages"):
        inputs = _parts_messages(attrs["gen_ai.input.messages"])
    if not outputs and attrs.get("gen_ai.output.messages"):
        outputs = _parts_messages(attrs["gen_ai.output.messages"])
    return inputs, outputs


def normalize_span(rec: dict[str, Any]) -> dict[str, Any] | None:
    from .evals import redact

    start, end = _num(rec.get("startTimeUnixNano")), _num(rec.get("endTimeUnixNano"))
    span_id = rec.get("spanId")
    if start is None or not span_id:
        return None
    if end is None:
        end = start + (_num(rec.get("durationNano")) or 0)
    attrs = rec.get("attributes") or {}
    name = str(rec.get("name") or "")
    cat = category(name, attrs)
    inputs, outputs = span_messages(rec)
    tokens_in = _num(attrs.get("gen_ai.usage.input_tokens"))
    tokens_out = _num(attrs.get("gen_ai.usage.output_tokens"))
    shown = {
        k: redact(_clip_text(v, ATTR_TEXT_MAX)) if isinstance(v, str | dict | list) else v
        for k, v in sorted(attrs.items())
        if k not in _HEAVY_ATTRS
    }
    status = str((rec.get("status") or {}).get("code") or "UNSET").upper()
    exceptions = [
        {
            "type": str(ea.get("exception.type") or ""),
            "message": redact(_clip_text(ea.get("exception.message") or "", ATTR_TEXT_MAX)),
            "stacktrace": redact(_clip_text(ea.get("exception.stacktrace") or "", 8_000)),
        }
        for ev in rec.get("events") or []
        if ev.get("name") == "exception"
        for ea in [ev.get("attributes") or {}]
    ]
    return {
        "span_id": str(span_id),
        "parent_id": str(rec.get("parentSpanId") or "") or None,
        "trace_id": rec.get("traceId"),
        "name": name,
        "category": cat,
        "start_ns": start,
        "end_ns": max(end, start),
        "status": "error" if "ERROR" in status or exceptions else "ok",
        "exceptions": exceptions,
        "model": attrs.get("gen_ai.request.model"),
        "tool": attrs.get("gen_ai.tool.name"),
        "tool_status": attrs.get("gen_ai.tool.status"),
        "tokens": {
            "input": int(tokens_in) if tokens_in is not None else None,
            "output": int(tokens_out) if tokens_out is not None else None,
        },
        "input_messages": inputs,
        "output_messages": outputs,
        "attributes": shown,
    }


def build_trace(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Span tree in waterfall order (depth-first, children by start time) with offsets
    relative to the whole session. Missing parents become roots; cycles are cut."""
    spans = {}
    for rec in records:
        s = normalize_span(rec)
        if s and s["span_id"] not in spans:
            spans[s["span_id"]] = s
    if not spans:
        return None
    t0 = min(s["start_ns"] for s in spans.values())
    t1 = max(s["end_ns"] for s in spans.values())
    total = max(t1 - t0, 1.0)
    children: dict[str | None, list[dict[str, Any]]] = {}
    for s in spans.values():
        parent = (
            s["parent_id"] if s["parent_id"] in spans and s["parent_id"] != s["span_id"] else None
        )
        children.setdefault(parent, []).append(s)
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(parent: str | None, depth: int) -> None:
        for s in sorted(children.get(parent, []), key=lambda x: x["start_ns"]):
            if s["span_id"] in seen:
                continue
            seen.add(s["span_id"])
            s["depth"] = depth
            ordered.append(s)
            walk(s["span_id"], depth + 1)

    walk(None, 0)
    for s in spans.values():  # anything only reachable through a cycle
        if s["span_id"] not in seen:
            seen.add(s["span_id"])
            s["depth"] = 0
            ordered.append(s)
    for s in ordered:
        s["start_ms"] = round((s["start_ns"] - t0) / 1e6, 3)
        s["duration_ms"] = round((s["end_ns"] - s["start_ns"]) / 1e6, 3)
        s["offset_pct"] = round((s["start_ns"] - t0) / total * 100, 4)
        s["width_pct"] = round((s["end_ns"] - s["start_ns"]) / total * 100, 4)
        del s["start_ns"], s["end_ns"]
    llm = [s for s in ordered if s["category"] == "llm"]
    return {
        "duration_ms": round(total / 1e6, 3),
        "started_at": datetime.fromtimestamp(t0 / 1e9, UTC).isoformat(),
        "totals": {
            "spans": len(ordered),
            "llm_calls": len(llm),
            "tool_calls": sum(1 for s in ordered if s["category"] == "tool"),
            "errors": sum(1 for s in ordered if s["status"] == "error"),
            # LLM spans only: agent/cycle spans carry the same usage aggregated again
            "input_tokens": sum(s["tokens"]["input"] or 0 for s in llm),
            "output_tokens": sum(s["tokens"]["output"] or 0 for s in llm),
        },
        "spans": ordered,
    }


# ---------------- eval sample trace ----------------

_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def sample_trace(eval_id: str, index: int, force: bool = False) -> dict[str, Any]:
    """{state: ready | pending | empty, reason?, trace?} for one eval sample."""
    from . import evals as esvc

    ev = esvc.load_eval(eval_id)
    if not ev.get("observe"):
        raise AppError(
            "eval.trace_unavailable",
            "this eval did not record traces",
            status=404,
        )
    key = (eval_id, index)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and not force and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    item = next((it for it in esvc.load_items(ev) if it["index"] == index), None)
    if item is None:
        raise AppError("eval.sample_not_found", f"sample {index} not found", status=404)
    sid = esvc.session_id_of(item)
    if not sid:
        return {"state": "empty", "reason": "no_session"}
    now = datetime.now(UTC)
    began = ev.get("started_at") or ev.get("created_at") or now
    ended = ev.get("ended_at")
    began = began if began.tzinfo else began.replace(tzinfo=UTC)
    ended = ended.replace(tzinfo=UTC) if ended and not ended.tzinfo else ended
    window_end = min(now, (ended or now) + timedelta(minutes=30))
    records = query_session_spans(ev["region"], sid, began - timedelta(minutes=5), window_end)
    trace = build_trace(records)
    if trace:
        out = {"state": "ready", "session_id": sid, "trace": trace}
    elif ended is None or now - ended < PENDING_WINDOW:
        out = {"state": "pending", "session_id": sid}  # still ingesting: refresh later
    else:
        out = {"state": "empty", "session_id": sid, "reason": "no_spans"}
    with _cache_lock:
        _cache[key] = (time.time(), out)
    return out
