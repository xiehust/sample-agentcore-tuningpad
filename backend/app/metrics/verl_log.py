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
    # Failed training rollouts (pipelines/run.py guards on both): dropped before any model
    # turn, and the per-session share the agent failed after one (trained at reward 0).
    "training/rollout_failure/total_missing_sessions",
    "agent_loop/acr_failed/mean",
    "val-aux/unknown/acr_failed/mean@1",
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


# verl: val-core|val-aux/<data_source>/<variable>/mean@<samples per Task>; data sources
# may contain slashes (e.g. openai/gsm8k), variables do not
_VAL = re.compile(r"^val-(?:core|aux)/(?P<src>.+)/(?P<var>[^/]+)/mean@(?P<n>\d+)$")


def by_source(step_metrics: dict[str, float]) -> dict[str, dict[str, float]]:
    """Validation means per data_source (a multi-Harness run's data_source is the Harness):
    {source: {variable: value}}, taking the mean over all of a Task's samples (the highest
    @N, i.e. val_n)."""
    best: dict[tuple[str, str], tuple[int, float]] = {}
    for key, value in step_metrics.items():
        if m := _VAL.match(key):
            k, n = (m["src"], m["var"]), int(m["n"])
            if k not in best or n > best[k][0]:
                best[k] = (n, value)
    out: dict[str, dict[str, float]] = {}
    for (src, var), (_, value) in best.items():
        out.setdefault(src, {})[var] = value
    return out


def _mean_over_sources(step_metrics: dict[str, float], var: str) -> float | None:
    values = [v[var] for v in by_source(step_metrics).values() if var in v]
    return sum(values) / len(values) if values else None


def derived_series(metrics: dict[int, dict[str, float]]) -> dict[str, list]:
    """Run-level validation curves whatever the data sources: `val/reward` and
    `val/acr_failed`, the mean over sources at each validation step."""
    out: dict[str, list] = {}
    for name, var in (("val/reward", "reward"), ("val/acr_failed", "acr_failed")):
        out[name] = [
            [step, v]
            for step in sorted(metrics)
            if (v := _mean_over_sources(metrics[step], var)) is not None
        ]
    return out


def source_series(metrics: dict[int, dict[str, float]]) -> dict[str, dict[str, list]]:
    """{source: {variable: [[step, value], ...]}} over a run's steps."""
    out: dict[str, dict[str, list]] = {}
    for step in sorted(metrics):
        for src, vars_ in by_source(metrics[step]).items():
            for var, value in vars_.items():
                out.setdefault(src, {}).setdefault(var, []).append([step, value])
    return out


def _val_reward(step_metrics: dict[str, float]) -> float | None:
    """The step's validation reward: the mean over data sources (one for most runs)."""
    return _mean_over_sources(step_metrics, "reward")


def summarize(metrics: dict[int, dict[str, float]]) -> dict[str, float | int | None]:
    if not metrics:
        return {"last_step": None}
    last = max(metrics)
    val = {s: r for s, m in metrics.items() if (r := _val_reward(m)) is not None}
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
