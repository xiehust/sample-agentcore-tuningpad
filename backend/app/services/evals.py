"""Per-sample view of an eval: state, transcript and task input, redacted for the console.

Rows come from `results.jsonl` (written by `pipelines/serving.py:stage_eval` from
`services.rollouts` records). They keep everything the agent returned; only an allowlist of
fields leaves this module, and every string is scanned for credentials (agents may echo
the endpoint API key from `_config`, or other secrets, into their transcripts).

Rows written before the toolkit v0.2 contract carry `rewards`, `status_code` and the S3
`result_key`; they are still read.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from pathlib import Path
from typing import Any

from ..core import aws
from ..core.db import session_scope
from ..core.errors import AppError, NotFound
from ..models import Dataset, Eval
from . import datasets as dsvc

STATES = ("scored", "truncated", "acr_failed", "invoke_failed", "unscored")
FAILED = {"acr_failed", "invoke_failed"}
TEXT_MAX = 20_000  # characters per text block
MSG_MAX = 200  # messages per transcript
TRACEBACK_MAX = 8_000  # tail of a traceback
PAGE_MAX = 100

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|secret|password|authorization|session[_-]?token)")
_SECRET_VAL = re.compile(
    r"\btp-[A-Za-z0-9_-]{20,}"  # TuningPad inference endpoint keys (services.serving.api_key)
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"  # AWS access key ids
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}"
)
_HIDDEN_RESULT_KEYS = {
    "payload",
    "_config",
    "_rollout",
    "s3_bucket",
    "result_key",
    "input_id",
}


# ---------------- state (shared with services.serving.summarize_eval) ----------------


def reward_of(result: dict[str, Any] | None) -> float | None:
    result = result or {}
    r = result.get("reward", result.get("rewards"))  # v0.2 scalar, v0.1 rows: rewards
    if isinstance(r, list):
        r = r[-1] if r else None
    if isinstance(r, int | float) and not isinstance(r, bool) and math.isfinite(r):
        return float(r)
    return None


def sample_state(item: dict[str, Any]) -> str:
    """scored | truncated | acr_failed | invoke_failed. A result without a usable reward is
    an agent failure; so is an agent that raised (`failure == "agent"`). v0.1 rows: a 500
    from the agent was still success=True, so the reward decides there too."""
    result = item.get("result") or {}
    if "max_tokens" in str(result.get("stop_reason") or ""):
        return "truncated"  # turn budget exhausted: scored 0, not an infra failure
    if not item.get("success"):
        return "acr_failed" if item.get("failure") == "agent" else "invoke_failed"
    if reward_of(result) is None and result.get("stop_reason") == "verifier_no_result":
        return "unscored"  # the Task's Verifier produced nothing: neither right nor wrong
    return "scored" if reward_of(result) is not None else "acr_failed"


# ---------------- the per-Harness matrix ----------------


def eval_cells(payloads: list[dict[str, Any]], harnesses: list[str] | None) -> list[dict[str, Any]]:
    """One cell per Task, or per Task × Harness (the eval's Harnesses replace a dataset's
    own column). Each cell is run once."""
    if not harnesses:
        return [{"task_index": i, "harness": None, "payload": p} for i, p in enumerate(payloads)]
    return [
        {"task_index": i, "harness": h, "payload": {**p, "harness": h}}
        for i, p in enumerate(payloads)
        for h in harnesses
    ]


def tool_calls_of(item: dict[str, Any]) -> int | None:
    """The Agent's own count (multi_harness metrics), else tool uses in the transcript."""
    n = ((item.get("result") or {}).get("metrics") or {}).get("tool_calls")
    if isinstance(n, int | float) and not isinstance(n, bool):
        return int(n)
    return None


def solved(item: dict[str, Any]) -> bool:
    if sample_state(item) != "scored":
        return False
    correct = ((item.get("result") or {}).get("metrics") or {}).get("correct")
    if correct is not None:
        return bool(correct)
    return (reward_of(item.get("result")) or 0.0) >= 1.0


def harness_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """pass@1 over the cells that could be graded (unscored ones excluded, failed ones count
    as unsolved), and mean tool calls over scored cells."""
    states = [sample_state(it) for it in items]
    unscored = states.count("unscored")
    graded = len(items) - unscored
    n_solved = sum(solved(it) for it in items)
    calls = [
        c
        for it, st in zip(items, states, strict=True)
        if st == "scored"
        if (c := tool_calls_of(it)) is not None
    ]
    return {
        "n": len(items),
        "scored": states.count("scored"),
        "unscored": unscored,
        "failed": sum(st in FAILED for st in states),
        "solved": n_solved,
        "pass_at_1": round(n_solved / graded, 4) if graded else None,
        "mean_tool_calls": round(sum(calls) / len(calls), 2) if calls else None,
    }


def by_harness(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        if it.get("harness"):
            groups.setdefault(it["harness"], []).append(it)
    return {h: harness_summary(g) for h, g in groups.items()} or None


def compare(items: list[dict[str, Any]], other: list[dict[str, Any]]) -> dict[str, Any]:
    """Per Harness: pass@1 of both evals, and tool calls on the Tasks both solved."""
    key = lambda it: (it.get("task_index", it["index"]), it.get("harness"))  # noqa: E731
    theirs = {key(it): it for it in other}
    out: dict[str, Any] = {}
    for h in sorted({it.get("harness") or "" for it in items + other}):
        mine = [it for it in items if (it.get("harness") or "") == h]
        their = [it for it in other if (it.get("harness") or "") == h]
        both = [
            (it, theirs[key(it)])
            for it in mine
            if key(it) in theirs and solved(it) and solved(theirs[key(it)])
        ]
        pairs = [(tool_calls_of(a), tool_calls_of(b)) for a, b in both]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        a_mean = round(sum(a for a, _ in pairs) / len(pairs), 2) if pairs else None
        b_mean = round(sum(b for _, b in pairs) / len(pairs), 2) if pairs else None
        out[h] = {
            "pass_at_1": harness_summary(mine)["pass_at_1"],
            "other_pass_at_1": harness_summary(their)["pass_at_1"],
            "both_solved": len(both),
            "mean_tool_calls": a_mean,
            "other_mean_tool_calls": b_mean,
            "tool_call_change": round(a_mean / b_mean - 1, 4)
            if a_mean is not None and b_mean
            else None,
        }
    return out


def session_id_of(item: dict[str, Any]) -> str | None:
    """runtimeSessionId of the sample: stored by stage_eval, else (v0.1 rows) the file name
    of the toolkit's result key `evals/<eval>/<input_id>/<session_id>.json`."""
    sid = (
        item.get("session_id") or Path(str((item.get("result") or {}).get("result_key") or "")).stem
    )
    try:
        return str(uuid.UUID(str(sid)))
    except ValueError:
        return None


# ---------------- redaction and clipping ----------------


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "***" if _SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VAL.sub("***", value)
    return value


def _clip(text: str, limit: int = TEXT_MAX, tail: bool = False) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return (text[-limit:] if tail else text[:limit]), True


def _as_text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2, default=str)


def _block(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        raw = {"other": raw}
    if "text" in raw:
        out: dict[str, Any] = {"type": "text", "text": _as_text(raw["text"])}
    elif "toolUse" in raw:
        tu = raw["toolUse"] or {}
        out = {
            "type": "tool_use",
            "name": str(tu.get("name") or ""),
            "tool_use_id": str(tu.get("toolUseId") or ""),
            "text": _as_text(tu.get("input", {})),
        }
    elif "toolResult" in raw:
        tr = raw["toolResult"] or {}
        parts = [
            _as_text(c.get("text") if "text" in c else c.get("json", c))
            for c in tr.get("content") or []
            if isinstance(c, dict)
        ]
        out = {
            "type": "tool_result",
            "tool_use_id": str(tr.get("toolUseId") or ""),
            "status": str(tr.get("status") or ""),
            "text": "\n".join(parts),
        }
    elif "reasoningContent" in raw:
        rc = raw["reasoningContent"] or {}
        out = {
            "type": "reasoning",
            "text": _as_text((rc.get("reasoningText") or {}).get("text", rc)),
        }
    else:
        out = {"type": "other", "text": _as_text(raw)}
    out["text"], clipped = _clip(redact(out["text"]))
    if clipped:
        out["truncated"] = True
    return out


def normalize_messages(messages: Any) -> tuple[list[dict[str, Any]], bool]:
    """Strands/Bedrock-style `{role, content: [blocks]}` (also plain-string content)."""
    msgs = messages if isinstance(messages, list) else []
    out = []
    for m in msgs[:MSG_MAX]:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        blocks = content if isinstance(content, list) else [content] if content else []
        out.append({"role": str(m.get("role") or "unknown"), "blocks": [_block(b) for b in blocks]})
    return out, len(msgs) > MSG_MAX


# ---------------- loading ----------------


def load_eval(eval_id: str) -> dict[str, Any]:
    with session_scope() as s:
        e = s.get(Eval, eval_id)
        if not e:
            raise NotFound("eval.not_found", f"eval {eval_id} not found")
        out = {c.name: getattr(e, c.name) for c in Eval.__table__.columns}
        d = s.get(Dataset, e.dataset_id)
        out["region"] = d.region if d else None
    return out


def results_path(eval_id: str) -> Path:
    return Path(dsvc.local_dir(eval_id)) / "results.jsonl"


def load_items(ev: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows of results.jsonl by index (the local copy grows while the eval runs; once it is
    gone, the S3 copy uploaded at the end is fetched and cached)."""
    path = results_path(ev["id"])
    if not path.exists() and ev.get("results_s3") and ev.get("region"):
        bucket, key = ev["results_s3"].removeprefix("s3://").split("/", 1)
        aws.client("s3", ev["region"]).download_file(bucket, key, str(path))
    rows: dict[int, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                it = json.loads(line)
            except ValueError:
                continue  # a half-written last line while the eval is running
            if isinstance(it, dict) and isinstance(it.get("index"), int):
                rows[it["index"]] = it
    return [rows[i] for i in sorted(rows)]


def split_frame(dataset_id: str, split: str):
    """The dataset split as a DataFrame (cached locally, downloaded from S3 once)."""
    with session_scope() as s:
        d = s.get(Dataset, dataset_id)
        if not d or split not in (d.splits or {}):
            return None
        region, key = d.region, d.splits[split]["s3_key"]
    local = Path(dsvc.local_dir(dataset_id)) / f"{split}.parquet"
    if not local.exists():
        from . import project as proj

        bucket = proj.require_region(region)["bucket"]
        aws.client("s3", region).download_file(bucket, key, str(local))
    import pandas as pd

    return pd.read_parquet(local)


# ---------------- views ----------------


def _counts(messages: Any) -> tuple[int, int]:
    turns = tools = 0
    for m in messages if isinstance(messages, list) else []:
        if isinstance(m, dict) and m.get("role") == "assistant":
            turns += 1
            tools += sum(
                1 for b in m.get("content") or [] if isinstance(b, dict) and "toolUse" in b
            )
    return turns, tools


def row_view(item: dict[str, Any]) -> dict[str, Any]:
    result = item.get("result") or {}
    turns, tools = _counts(result.get("messages"))
    if (counted := tool_calls_of(item)) is not None:
        tools = counted
    elapsed = item.get("elapsed")
    return {
        "index": item["index"],
        "task_index": item.get("task_index", item["index"]),
        "harness": item.get("harness"),
        "state": sample_state(item),
        "reward": reward_of(result),
        "status_code": result.get("status_code"),
        "stop_reason": _clip(redact(str(result.get("stop_reason") or "")), 300)[0] or None,
        "error": _clip(redact(str(item.get("error") or "")), 300)[0] or None,
        "turns": turns,
        "tool_calls": tools,
        "elapsed_s": round(float(elapsed), 1) if isinstance(elapsed, int | float) else None,
        "session_id": session_id_of(item),
        "has_transcript": bool(result.get("messages")),
    }


def list_samples(eval_id: str, status: str = "all", offset: int = 0, limit: int = 50):
    ev = load_eval(eval_id)
    rows = [row_view(it) for it in load_items(ev)]
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in STATES}
    if status == "failed":
        rows = [r for r in rows if r["state"] in FAILED]
    elif status == "truncated":
        rows = [r for r in rows if r["state"] == "truncated"]
    limit = max(1, min(limit, PAGE_MAX))
    return {
        "total": len(rows),
        "counts": counts,
        "observe": bool(ev.get("observe")),
        "samples": rows[max(0, offset) : max(0, offset) + limit],
    }


def task_input(dataset_id: str, split: str, index: int) -> dict[str, Any]:
    """What the agent was asked: the explicit prompt column, else readable payload fields."""
    try:
        df = split_frame(dataset_id, split)
    except Exception:  # dataset gone / S3 unreachable: the transcript is still worth showing
        df = None
    if df is None or index >= len(df):
        return {"prompt": None, "fields": {}}
    row = df.iloc[index]
    prompt = None
    if "prompt" in df.columns:
        p = dsvc._to_jsonable(row["prompt"])
        if isinstance(p, list):
            p = "\n\n".join(
                _as_text(m.get("content"))
                for m in p
                if isinstance(m, dict) and m.get("role") == "user"
            )
        prompt = _as_text(p) if p else None
    payload = dsvc._to_jsonable(row["payload"]) if "payload" in df.columns else {}
    fields = {
        str(k): _clip(redact(str(v)), 2000)[0]
        for k, v in (payload.items() if isinstance(payload, dict) else [])
        if isinstance(v, str | int | float | bool) and not _SECRET_KEY.search(str(k))
    }
    return {"prompt": _clip(redact(prompt))[0] if prompt else None, "fields": fields}


def sample_detail(eval_id: str, index: int) -> dict[str, Any]:
    ev = load_eval(eval_id)
    item = next((it for it in load_items(ev) if it["index"] == index), None)
    if item is None:
        raise NotFound("eval.sample_not_found", f"sample {index} not found")
    result = item.get("result") or {}
    messages, more = normalize_messages(result.get("messages"))
    tb = str(result.get("traceback") or "")
    extra = {
        k: redact(v)
        for k, v in result.items()
        if k not in _HIDDEN_RESULT_KEYS
        and k not in {"messages", "traceback", "reward", "rewards", "status_code", "stop_reason"}
        and isinstance(v, str | int | float | bool)
    }
    return {
        **row_view(item),
        "input": task_input(ev["dataset_id"], ev["split"], item.get("task_index", index)),
        "messages": messages,
        "messages_truncated": more,
        "traceback": _clip(redact(tb), TRACEBACK_MAX, tail=True)[0] or None,
        "extra": extra,
    }


def compare_evals(eval_id: str, other_id: str) -> dict[str, Any]:
    ev, other = load_eval(eval_id), load_eval(other_id)
    if (ev["dataset_id"], ev["split"]) != (other["dataset_id"], other["split"]):
        raise AppError(
            "eval.not_comparable", "only evals of the same dataset split can be compared"
        )
    return {
        "eval_id": eval_id,
        "other_id": other_id,
        "by_harness": compare(load_items(ev), load_items(other)),
    }
