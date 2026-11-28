"""Reward: the Verifier's score, plus a small tool-efficiency bonus for correct answers."""

from __future__ import annotations


def score(
    verifier_reward: float | None, tool_calls: int, *, cap: float, budget: int
) -> float | None:
    """None when the Verifier produced no result (unscorable, not wrong). A correct answer
    (Verifier score >= 1) earns up to `cap` more, shrinking linearly to 0 at `budget` tool
    calls; anything else keeps the Verifier's score, so giving up early earns nothing."""
    if verifier_reward is None:
        return None
    if verifier_reward < 1.0:
        return float(verifier_reward)
    used = min(max(tool_calls, 0), budget) / budget if budget > 0 else 1.0
    return float(verifier_reward) + cap * (1.0 - used)
