import time

import pytest

from app.core.db import session_scope
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
    eng.register("t.resume", [eng.Stage("only", lambda ctx: hits.append(1))])
    with session_scope() as s:
        s.add(
            Job(
                id="job-r",
                type="t.resume",
                status="running",
                stages=[{"name": "only", "status": "running"}],
                payload={},
                context={},
            )
        )
    assert eng.get_engine().resume_pending() == ["job-r"]
    assert _wait("job-r").status == "succeeded"
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
