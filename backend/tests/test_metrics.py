from pathlib import Path

import pytest

from app.metrics import verl_log

SAMPLE = (Path(__file__).parent / "fixtures" / "verl_train_sample.log").read_text()


def test_parses_real_verl_lines():
    recs, carry = verl_log.parse_chunk("", SAMPLE)
    by_step = dict(recs)
    assert set(by_step) == {0, 1, 10, 60}
    assert by_step[0]["val-core/unknown/reward/mean@1"] == 0.545  # np.float64(0.545)
    assert by_step[1]["critic/score/mean"] == 0.2578125
    assert by_step[10]["val-core/unknown/reward/mean@1"] == 0.85
    assert by_step[60]["val-core/unknown/reward/mean@1"] == 0.795
    assert by_step[60]["batching/total_real_rows"] == 256
    assert by_step[60]["training/rollout_failure/total_missing_sessions"] == 0
    assert carry == ""


def test_chunk_boundary_carries_partial_line():
    line = next(x for x in SAMPLE.split("\n") if "step:10 -" in x) + "\n"
    a, b = line[:300], line[300:]
    recs1, carry = verl_log.parse_chunk("", a)
    assert recs1 == [] and carry == a
    recs2, carry = verl_log.parse_chunk(carry, b)
    assert recs2[0][0] == 10 and carry == ""


def test_summary_matches_report():
    recs, _ = verl_log.parse_chunk("", SAMPLE)
    s = verl_log.summarize(dict(recs))
    # REPORT.md: 0.545 → best 0.85 (step 10) → 0.795 (step 60)
    assert s["baseline_val_reward"] == 0.545
    assert s["best_val_reward"] == 0.85 and s["best_step"] == 10
    assert s["val_reward"] == 0.795 and s["last_step"] == 60


def test_ignores_noise_and_nan():
    assert verl_log.parse_line("INFO starting ray") is None
    r = verl_log.parse_line("step:3 - a:nan - b:1e-3 - c:np.float32(2.5) - bad")
    assert r == (3, {"b": 0.001, "c": 2.5})


def test_parses_toolkit_rollout_failure_metrics():
    line = (
        "\x1b[36m(TaskRunnerV1 pid=5)\x1b[0m step:7 - training/rollout_failure/total_missing_sessions:3"
        " - agent_loop/acr_failed/mean:np.float64(0.0234375)"
    )
    step, m = verl_log.parse_line(line)
    assert step == 7
    assert m == {
        "training/rollout_failure/total_missing_sessions": 3,
        "agent_loop/acr_failed/mean": 0.0234375,
    }
    assert set(m) <= set(verl_log.KEY_METRICS)  # charted on the Runs health panel


def test_validation_reward_averages_over_data_sources():
    """Multi-Harness runs set data_source to the Harness, so verl reports one validation
    reward per Harness: the run's validation reward is their mean."""
    from app.metrics import verl_log

    metrics = {
        0: {"val-core/claude_code/reward/mean@1": 0.2, "val-core/opencode/reward/mean@1": 0.4},
        5: {
            "val-core/claude_code/reward/mean@1": 0.6,
            "val-core/opencode/reward/mean@1": 0.8,
            "critic/score/mean": 0.5,
        },
    }
    s = verl_log.summarize(metrics)
    assert s["baseline_val_reward"] == pytest.approx(0.3)
    assert s["val_reward"] == pytest.approx(0.7) and s["best_step"] == 5
    assert verl_log.by_source(metrics[5]) == {
        "claude_code": {"reward": 0.6},
        "opencode": {"reward": 0.8},
    }


def test_validation_metrics_use_the_highest_sample_count():
    """verl names validation means mean@{val_n}; sources may contain slashes."""
    step = {
        "val-core/openai/gsm8k/reward/mean@4": 0.5,
        "val-aux/openai/gsm8k/reward/mean@2": 0.1,
        "val-aux/openai/gsm8k/acr_failed/mean@4": 0.25,
        "val-aux/num_turns/mean": 3.0,
    }
    assert verl_log.by_source(step) == {"openai/gsm8k": {"reward": 0.5, "acr_failed": 0.25}}


def test_derived_validation_series_average_over_sources():
    metrics = {
        0: {"val-core/a/reward/mean@1": 0.2, "val-core/b/reward/mean@1": 0.4},
        5: {"val-aux/a/acr_failed/mean@1": 0.1, "val-aux/b/acr_failed/mean@1": 0.3},
    }
    s = verl_log.derived_series(metrics)
    assert s["val/reward"] == [[0, pytest.approx(0.3)]]
    assert s["val/acr_failed"] == [[5, pytest.approx(0.2)]]
