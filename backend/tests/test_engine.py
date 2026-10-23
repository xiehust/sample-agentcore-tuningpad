import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect

from app.core import db
from app.core.db import session_scope, utcnow
from app.core.errors import AppError
from app.jobs import engine as eng
from app.models import Job


def _wait(job_id, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job.status in eng.TERMINAL:
                return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _wait_status(job_id, status, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job.status == status:
                return job
        time.sleep(0.01)
    raise AssertionError(f"job did not become {status}; last status: {job.status}")


def test_stages_run_in_order_and_persist_context():
    seen = []

    def a(ctx):
        seen.append("a")
        ctx.set("x", 1)

    def b(ctx):
        seen.append(("b", ctx.context["x"]))
        ctx.log("hello")

    eng.register("t.order", [eng.Stage("a", a), eng.Stage("b", b)])
    job = _wait(eng.get_engine().start("t.order", "tgt"))
    assert job.status == "succeeded"
    assert seen == ["a", ("b", 1)]
    assert [s["status"] for s in job.stages] == ["succeeded", "succeeded"]
    assert "hello" in eng.read_log(job.id)["content"]


def test_failure_records_code_and_retry_resumes_from_failed_stage():
    calls = {"a": 0, "b": 0}

    def a(ctx):
        calls["a"] += 1

    def b(ctx):
        calls["b"] += 1
        if calls["b"] == 1:
            raise AppError("x.boom", "boom")

    eng.register("t.retry", [eng.Stage("a", a), eng.Stage("b", b)])
    e = eng.get_engine()
    job = _wait(e.start("t.retry"))
    assert job.status == "failed" and job.error_code == "x.boom"
    e.retry(job.id)
    job = _wait(job.id)
    assert job.status == "succeeded"
    assert calls == {"a": 1, "b": 2}  # stage a not re-run


def test_cancel_interrupts_sleep():
    def slow(ctx):
        ctx.sleep(30)

    eng.register("t.cancel", [eng.Stage("slow", slow)])
    e = eng.get_engine()
    jid = e.start("t.cancel")
    time.sleep(0.3)
    e.cancel(jid)
    assert _wait(jid).status == "cancelled"


def test_resume_pending_reruns_running_jobs():
    hits = []
    started_at = (utcnow() - timedelta(hours=1)).isoformat()
    eng.register("t.resume", [eng.Stage("only", lambda ctx: hits.append(1))])
    with session_scope() as s:
        s.add(
            Job(
                id="job-r",
                type="t.resume",
                status="running",
                stages=[{"name": "only", "status": "running", "started_at": started_at}],
                payload={},
                context={},
            )
        )
    assert eng.get_engine().resume_pending() == ["job-r"]
    job = _wait("job-r")
    assert job.status == "succeeded"
    assert job.stages[0]["started_at"] == started_at
    assert hits == [1]


def test_wait_until_timeout():
    def stage(ctx):
        ctx.wait_until(lambda: False, timeout_s=0.2, interval_s=0.05, what="never")

    eng.register("t.timeout", [eng.Stage("w", stage)])
    job = _wait(eng.get_engine().start("t.timeout"))
    assert job.error_code == "job.timeout"


def test_log_offsets():
    eng.append_log("job-x", "one")
    first = eng.read_log("job-x")
    eng.append_log("job-x", "two")
    second = eng.read_log("job-x", first["next_offset"])
    assert (
        "one" in first["content"] and "two" in second["content"] and "one" not in second["content"]
    )


def test_duplicate_stage_names_rejected():
    with pytest.raises(ValueError):
        eng.register("t.dup", [eng.Stage("a", lambda c: None), eng.Stage("a", lambda c: None)])


def test_deferred_stage_releases_worker_and_exposes_persisted_wait(client):
    finished = []

    def defer(ctx):
        ctx.set("resource_ids", ["resource-1"])
        ctx.defer(
            delay_s=3600,
            reason="test.resource_pending",
            detail="Waiting for one resource",
            detail_params={"count": 1},
        )

    eng.get_engine().shutdown()
    engine = eng.JobEngine(max_workers=1)
    eng.set_engine(engine)
    eng.register(
        "t.defer",
        [eng.Stage("wait", defer)],
        on_finish=lambda *args: finished.append(args),
    )
    eng.register("t.other", [eng.Stage("work", lambda ctx: None)])
    jid = engine.start("t.defer", "target-1")
    waiting = _wait_status(jid, "waiting")
    assert waiting.next_attempt_at > utcnow().replace(tzinfo=None) + timedelta(minutes=59)
    assert waiting.context == {"resource_ids": ["resource-1"]}
    assert waiting.error is None and waiting.ended_at is None
    assert waiting.stages[0]["reason"] == "test.resource_pending"
    assert waiting.stages[0]["detail_params"] == {"count": 1}
    assert waiting.stages[0]["next_attempt_at"].endswith("+00:00")
    assert finished == []
    assert "waiting" in eng.ACTIVE

    # The only worker can finish another job while this job waits for an hour.
    assert _wait(engine.start("t.other")).status == "succeeded"
    with pytest.raises(AppError) as exc:
        engine.retry(jid)
    assert exc.value.code == "job.active"
    response = client.get(f"/api/jobs/{jid}").json()
    assert response["status"] == "waiting"
    assert response["stages"][0]["detail"] == "Waiting for one resource"
    assert datetime.fromisoformat(response["next_attempt_at"]).utcoffset() == timedelta(0)


def test_restart_preserves_future_due_date_and_resumes_overdue_once(monkeypatch):
    hits = []

    def check(ctx):
        hits.append("check")
        if not ctx.context.get("requested"):
            ctx.set("requested", True)
            ctx.defer(delay_s=3600, reason="test.pending")
        assert ctx.context["requested"] is True

    eng.register(
        "t.defer_restart",
        [eng.Stage("before", lambda ctx: hits.append("before")), eng.Stage("check", check)],
    )
    old = eng.get_engine()
    jid = old.start("t.defer_restart", run_inline=True)
    waiting = _wait_status(jid, "waiting")
    due = waiting.next_attempt_at
    old.shutdown()
    engine = eng.JobEngine(max_workers=1)
    eng.set_engine(engine)

    assert engine.resume_pending() == []
    assert engine.resume_due() == []
    with session_scope() as s:
        assert s.get(Job, jid).next_attempt_at == due
    assert hits == ["before", "check"]

    monkeypatch.setattr(eng, "utcnow", lambda: due.replace(tzinfo=UTC) + timedelta(seconds=1))
    assert engine.resume_pending() == [jid]
    assert _wait(jid).status == "succeeded"
    assert engine.resume_pending() == []
    assert engine.resume_due() == []
    assert hits == ["before", "check", "check"]
    with session_scope() as s:
        assert s.get(Job, jid).next_attempt_at is None


def test_stage_duration_includes_repeated_waits_and_backend_restart(monkeypatch):
    clock = datetime(2026, 10, 19, tzinfo=UTC)
    first_check_at = clock
    monkeypatch.setattr(eng, "utcnow", lambda: clock)

    def check(ctx):
        checks = ctx.context.get("checks", 0) + 1
        ctx.set("checks", checks)
        if checks < 3:
            ctx.defer(delay_s=3600, reason="test.pending")

    eng.register("t.wait_duration", [eng.Stage("check", check)])
    old = eng.get_engine()
    jid = old.start("t.wait_duration", run_inline=True)
    assert _wait_status(jid, "waiting").stages[0]["started_at"] == first_check_at.isoformat()

    clock += timedelta(hours=5)
    assert old.resume_due() == [jid]
    waiting = _wait_status(jid, "waiting")
    assert waiting.context["checks"] == 2
    assert waiting.stages[0]["started_at"] == first_check_at.isoformat()
    old.shutdown()
    engine = eng.JobEngine(max_workers=1)
    eng.set_engine(engine)
    assert engine.resume_pending() == []

    clock += timedelta(hours=5)
    assert engine.resume_pending() == [jid]
    job = _wait(jid)
    assert job.status == "succeeded"
    stage = job.stages[0]
    assert stage["started_at"] == first_check_at.isoformat()
    assert datetime.fromisoformat(stage["ended_at"]) - datetime.fromisoformat(
        stage["started_at"]
    ) == timedelta(hours=10)
    assert job.context["checks"] == 3


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_explicit_retry_restarts_terminal_stage_duration(monkeypatch, status):
    clock = datetime(2026, 10, 19, tzinfo=UTC)
    hits = []
    monkeypatch.setattr(eng, "utcnow", lambda: clock)

    def check(ctx):
        hits.append(1)
        if len(hits) == 1:
            if status == "failed":
                raise AppError("test.failure", "retry this stage")
            raise eng.JobCancelled()

    eng.register(f"t.retry_duration_{status}", [eng.Stage("check", check)])
    engine = eng.get_engine()
    jid = engine.start(f"t.retry_duration_{status}", run_inline=True)
    first = _wait(jid)
    assert first.status == status
    assert first.stages[0]["started_at"] == clock.isoformat()

    clock += timedelta(hours=1)
    engine.retry(jid)
    job = _wait(jid)
    assert job.status == "succeeded"
    assert job.stages[0]["started_at"] == clock.isoformat()
    assert hits == [1, 1]


def test_scheduler_automatically_resumes_without_reconciler():
    seen = []

    def check(ctx):
        seen.append(1)
        if len(seen) == 1:
            ctx.defer(delay_s=0.05, reason="test.pending")

    eng.register("t.auto_resume", [eng.Stage("check", check)])
    engine = eng.get_engine()
    engine.start_scheduler(interval_s=0.01)
    engine.start_scheduler(interval_s=0.01)
    jid = engine.start("t.auto_resume")
    job = _wait(jid)
    assert job.status == "succeeded"
    assert job.next_attempt_at is None
    assert job.stages[0]["reason"] is None
    assert job.stages[0]["next_attempt_at"] is None
    assert seen == [1, 1]


def test_cancelled_wait_is_terminal_across_restart(monkeypatch):
    finished = []
    hits = []

    def check(ctx):
        hits.append(1)
        ctx.defer(delay_s=3600, reason="test.pending")

    eng.register(
        "t.cancel_wait",
        [eng.Stage("check", check)],
        on_finish=lambda *args: finished.append(args),
    )
    old = eng.get_engine()
    jid = old.start("t.cancel_wait", "target-1", run_inline=True)
    old.cancel(jid)
    job = _wait(jid)
    assert job.status == "cancelled"
    assert job.next_attempt_at is None
    assert job.stages[0]["status"] == "cancelled"
    assert job.stages[0]["next_attempt_at"] is None
    assert finished == [("target-1", "cancelled", None)]
    old.shutdown()
    engine = eng.JobEngine(max_workers=1)
    eng.set_engine(engine)
    future = utcnow() + timedelta(days=1)
    monkeypatch.setattr(eng, "utcnow", lambda: future)
    assert engine.resume_pending() == []
    assert engine.resume_due() == []
    assert hits == [1]


def test_cancel_wins_when_stage_is_entering_wait(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    engine = eng.get_engine()
    original = engine._defer

    def pause_defer(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(engine, "_defer", pause_defer)
    eng.register(
        "t.cancel_race",
        [eng.Stage("check", lambda ctx: ctx.defer(delay_s=1, reason="test.pending"))],
    )
    jid = engine.start("t.cancel_race")
    try:
        assert entered.wait(5)
        engine.cancel(jid)
    finally:
        release.set()
    job = _wait(jid)
    assert job.status == "cancelled"
    assert job.next_attempt_at is None
    assert job.stages[0]["status"] == "cancelled"
    assert engine.resume_due() == []


def test_competing_schedulers_claim_a_due_wait_once():
    entered, release = threading.Event(), threading.Event()
    barrier = threading.Barrier(2)
    hits = []

    def check(ctx):
        hits.append(1)
        entered.set()
        assert release.wait(5)

    eng.register("t.claim", [eng.Stage("check", check)])
    with session_scope() as s:
        s.add(
            Job(
                id="job-claim",
                type="t.claim",
                status="waiting",
                stages=[{"name": "check", "status": "waiting"}],
                payload={},
                context={},
                next_attempt_at=utcnow() - timedelta(seconds=1),
            )
        )
    other = eng.JobEngine(max_workers=1)
    engines = [eng.get_engine(), other]

    def schedule(engine):
        barrier.wait(timeout=5)
        return engine.resume_due()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(schedule, engines))
        assert sorted(attempts) == [[], ["job-claim"]]
        assert entered.wait(5)
        assert [engine.resume_due() for engine in engines] == [[], []]
        release.set()
        assert _wait("job-claim").status == "succeeded"
    finally:
        release.set()
        other.shutdown()
    assert hits == [1]


def test_resume_merges_new_stages_without_replaying_completed_work():
    seen = []
    eng.register(
        "t.upgraded_pipeline",
        [
            eng.Stage("original", lambda ctx: pytest.fail("completed stage must not replay")),
            eng.Stage("new_cleanup", lambda ctx: seen.append(("new", ctx.context["arn"]))),
            eng.Stage("last", lambda ctx: seen.append("last")),
        ],
    )
    with session_scope() as s:
        s.add(
            Job(
                id="job-upgrade",
                type="t.upgraded_pipeline",
                status="running",
                stages=[
                    {"name": "original", "status": "succeeded", "detail": "kept"},
                    {"name": "last", "status": "running"},
                ],
                payload={},
                context={"arn": "resource-1"},
            )
        )
    assert eng.get_engine().resume_pending() == ["job-upgrade"]
    job = _wait("job-upgrade")
    assert job.status == "succeeded"
    assert [stage["name"] for stage in job.stages] == ["original", "new_cleanup", "last"]
    assert [stage["status"] for stage in job.stages] == ["succeeded"] * 3
    assert job.stages[0]["detail"] == "kept"
    assert seen == [("new", "resource-1"), "last"]


def test_failed_deferred_check_stays_failed_until_explicit_retry(monkeypatch):
    hits = []

    def check(ctx):
        hits.append(1)
        if len(hits) == 1:
            ctx.defer(delay_s=0.01, reason="test.pending")
        if len(hits) == 2:
            raise RuntimeError("unrelated failure")

    eng.register("t.deferred_failure", [eng.Stage("check", check)])
    engine = eng.get_engine()
    jid = engine.start("t.deferred_failure", run_inline=True)
    future = utcnow() + timedelta(seconds=1)
    monkeypatch.setattr(eng, "utcnow", lambda: future)
    assert engine.resume_due() == [jid]
    job = _wait(jid)
    assert job.status == "failed"
    assert job.error_code == "internal"
    assert job.stages[0]["status"] == "failed"
    assert job.next_attempt_at is None
    assert engine.resume_due() == []
    engine.retry(jid)
    assert _wait(jid).status == "succeeded"
    assert len(hits) == 3


@pytest.mark.parametrize("mode", ["retry", "due"])
def test_new_attempt_is_not_lost_before_previous_worker_returns(monkeypatch, mode):
    ready, release = threading.Event(), threading.Event()
    hits = []
    engine = eng.get_engine()
    original = engine._run

    def check(ctx):
        hits.append(1)
        if len(hits) == 1:
            if mode == "retry":
                raise AppError("test.failure", "retry this stage")
            ctx.defer(delay_s=0.01, reason="test.pending")

    def pause_after_run(jid):
        original(jid)
        if len(hits) == 1:
            ready.set()
            assert release.wait(5)

    monkeypatch.setattr(engine, "_run", pause_after_run)
    eng.register(f"t.late_{mode}", [eng.Stage("check", check)])
    jid = engine.start(f"t.late_{mode}")
    try:
        assert ready.wait(5)
        if mode == "retry":
            engine.retry(jid)
        else:
            future = utcnow() + timedelta(seconds=1)
            monkeypatch.setattr(eng, "utcnow", lambda: future)
            assert engine.resume_due() == [jid]
    finally:
        release.set()
    assert _wait(jid).status == "succeeded"
    assert hits == [1, 1]


def test_shutdown_interrupts_polling_without_cancelling_or_resubmitting():
    entered = threading.Event()
    hits = []

    def check(ctx):
        hits.append(1)
        entered.set()
        ctx.sleep(60)

    eng.register("t.shutdown", [eng.Stage("check", check)])
    engine = eng.get_engine()
    jid = engine.start("t.shutdown")
    assert entered.wait(5)
    engine.shutdown()
    engine._pool.shutdown(wait=True)
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.status == "running" and job.error is None
        assert job.stages[0]["status"] == "running"
        job.status = "waiting"
        job.next_attempt_at = utcnow() - timedelta(seconds=1)
    assert engine.resume_due() == []
    assert engine.resume_pending() == []
    assert hits == [1]


def test_old_job_schema_adds_nullable_next_attempt_at(tmp_path):
    path = tmp_path / "old-jobs.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE jobs (
                id VARCHAR(64) PRIMARY KEY, type VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL, stages JSON NOT NULL, payload JSON NOT NULL,
                context JSON NOT NULL, workspace_id VARCHAR(64) NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO jobs VALUES (
                'job-old', 't.old', 'queued', '[]', '{}', '{}', 'default',
                '2026-10-19 00:00:00', '2026-10-19 00:00:00'
            )"""
        )
    db.init_db(f"sqlite:///{path}")
    column = next(
        c for c in inspect(db._engine).get_columns("jobs") if c["name"] == "next_attempt_at"
    )
    assert column["nullable"] is True
    with session_scope() as s:
        job = s.get(Job, "job-old")
        assert job.status == "queued"
        assert job.next_attempt_at is None
        assert eng.job_view(job)["next_attempt_at"] is None
