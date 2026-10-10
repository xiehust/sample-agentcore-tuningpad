"""Parse verl console-logger step lines into metrics.

Verified format (experiments/qwen35_2b_gsm8k train_full.log):
  \\x1b[36m(TaskRunnerV1 pid=92813)\\x1b[0m step:10 - key:value - key:np.float64(0.85) - ...
Lines may be split across log chunks; callers keep the trailing partial line.
"""

from __future__ import annotations

import re

ANSI = re.compile(r"\x1b\[[0-9;]*m")
STEP = re.compile(r"\bstep:(\d+) - (.*)$")
VALUE = re.compile(r"^(?:np\.\w+\()?(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|nan|inf|-inf)\)?$")

# Metrics the console charts (all others are still stored).
KEY_METRICS = [
    "val-core/unknown/reward/mean@1",
    "critic/score/mean",
    "actor/kl_loss",
    "actor/pg_loss",
    "actor/entropy",
    "actor/grad_norm",
    "response_length/mean",
    "training/rollout_failure/total_missing_sessions",
    "val-aux/unknown/acr_failed/mean@1",
    # Toolkit RolloutFailureGuardMixin (agentcore_sync, training rollouts only): dropped
    # rollouts are total_missing_sessions; these count the ones retried / kept at reward 0.
    "training/rollout_failure/total_transient_retry",
    "training/rollout_failure/total_model_train",
    "training/rollout_failure/drop_fraction",
    "batching/total_real_rows",
    "critic/advantages/zero_mean",
    "timing_s/step",
    "timing_s/gen",
    "training/global_step",
]


def parse_line(line: str) -> tuple[int, dict[str, float]] | None:
    m = STEP.search(ANSI.sub("", line).rstrip())
    if not m:
        return None
    step = int(m.group(1))
    out: dict[str, float] = {}
    for part in m.group(2).split(" - "):
        key, sep, raw = part.rpartition(":")
        if not sep or not key:
            continue
        vm = VALUE.match(raw.strip())
        if not vm:
            continue
        try:
            v = float(vm.group(1))
        except ValueError:
            continue
        if v == v and v not in (float("inf"), float("-inf")):  # drop nan/inf
            out[key.strip()] = v
    return (step, out) if out else None


def parse_chunk(carry: str, chunk: str) -> tuple[list[tuple[int, dict[str, float]]], str]:
    """Parse complete lines of `carry + chunk`; return (records, new_carry)."""
    text = carry + chunk
    lines = text.split("\n")
    carry_out = lines.pop()  # partial last line (or "")
    recs = [r for r in (parse_line(line) for line in lines) if r]
    return recs, carry_out


def summarize(metrics: dict[int, dict[str, float]]) -> dict[str, float | int | None]:
    if not metrics:
        return {"last_step": None}
    last = max(metrics)
    val = {
        s: m["val-core/unknown/reward/mean@1"]
        for s, m in metrics.items()
        if "val-core/unknown/reward/mean@1" in m
    }
    best_step = max(val, key=lambda s: val[s]) if val else None
    timing = [m["timing_s/step"] for m in metrics.values() if "timing_s/step" in m]
    return {
        "last_step": last,
        "val_reward": val.get(max(val)) if val else None,
        "best_val_reward": val.get(best_step) if best_step is not None else None,
        "best_step": best_step,
        "baseline_val_reward": val.get(0),
        "train_score": metrics[last].get("critic/score/mean"),
        "sec_per_step": round(sum(timing[-5:]) / len(timing[-5:]), 1) if timing else None,
    }
