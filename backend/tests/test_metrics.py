from pathlib import Path

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
