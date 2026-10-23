"""Staged pipeline engine (launchpad deploy-pipeline / Skill Lab pattern).

* A pipeline is an ordered list of named stages registered per job type.
* Every stage MUST be idempotent: it inspects real AWS/K8s state and only then
  acts, so a restarted backend can re-run the first unfinished stage safely.
* Stage outputs that later stages need go into `ctx.context` (persisted).
* Logs are plain text lines in data/jobs/<id>.log, read incrementally by offset.

Reconcilers are periodic background callables (status sync of long-lived
objects such as RayJobs or inference Deployments).
"""

from __future__ import annotations

import logging
import math
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy import func, or_

from ..core.config import get_settings
from ..core.db import new_id, session_scope, utcnow
from ..core.errors import AppError, NotFound
from ..models import Job

log = logging.getLogger(__name__)

TERMINAL = {"succeeded", "failed", "cancelled"}
ACTIVE = {"queued", "running", "waiting"}
LOG_READ_LIMIT = 256 * 1024


class JobCancelled(Exception):
    pass


class JobInterrupted(Exception):
    """The backend is stopping; keep this job resumable instead of cancelling it."""


@dataclass
class JobDeferred(Exception):
    delay_s: float
    reason: str
    detail: str | None
    detail_params: dict[str, Any] | None


@dataclass(frozen=True)
class Stage:
    name: str
    fn: Callable[[StageContext], None]


_PIPELINES: dict[str, list[Stage]] = {}
_HOOKS: dict[str, Callable[[str, str, str | None], None]] = {}


def register(
    job_type: str,
    stages: list[Stage],
    on_finish: Callable[[str, str, str | None], None] | None = None,
) -> None:
    """`on_finish(target_id, status, error)` runs when the job ends, just before its terminal
    status is persisted."""
    names = [s.name for s in stages]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate stage names in {job_type}")
    _PIPELINES[job_type] = stages
    if on_finish:
        _HOOKS[job_type] = on_finish


def pipeline(job_type: str) -> list[Stage]:
    if job_type not in _PIPELINES:
        raise AppError("job.unknown_type", f"no pipeline registered for {job_type}")
    return _PIPELINES[job_type]


def _pending_stage(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pending",
        "detail": None,
        "started_at": None,
        "ended_at": None,
    }


def log_path(job_id: str) -> Path:
    d = get_settings().data_dir / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{job_id}.log"


def append_log(job_id: str, message: str) -> None:
    ts = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path(job_id).open("a", encoding="utf-8") as f:
        for line in str(message).splitlines() or [""]:
            f.write(f"{ts} {line}\n")


def read_log(job_id: str, offset: int = 0, limit: int = LOG_READ_LIMIT) -> dict[str, Any]:
    p = log_path(job_id)
    if not p.exists():
        return {"content": "", "next_offset": 0, "eof": True}
    size = p.stat().st_size
    offset = max(0, min(offset, size))
    with p.open("rb") as f:
        f.seek(offset)
        chunk = f.read(limit)
    nxt = offset + len(chunk)
    return {
        "content": chunk.decode("utf-8", errors="replace"),
        "next_offset": nxt,
        "eof": nxt >= size,
    }


class StageContext:
    def __init__(
        self,
        engine: JobEngine,
        job_id: str,
        job_type: str,
        target_id: str | None,
        payload: dict,
        context: dict,
    ):
        self.engine = engine
        self.job_id = job_id
        self.job_type = job_type
        self.target_id = target_id
        self.payload = payload
        self.context = context
        self.stage = ""

    def log(self, message: str) -> None:
        append_log(self.job_id, f"[{self.stage}] {message}")

    def set(self, key: str, value: Any) -> None:
        """Persist a durable output for later stages / resume."""
        self.context[key] = value
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if job:
                job.context = {**(job.context or {}), key: value}

    def detail(self, text: str) -> None:
        """Short human-readable progress for the current stage (shown on the stage bar)."""
        self.engine._update_stage(self.job_id, self.stage, detail=text)

    def check_cancel(self) -> None:
        if self.engine.is_cancel_requested(self.job_id):
            raise JobCancelled()
        if self.engine._stop.is_set():
            raise JobInterrupted()

    def defer(
        self,
        *,
        delay_s: float,
        reason: str,
        detail: str | None = None,
        detail_params: dict[str, Any] | None = None,
    ) -> NoReturn:
        """Persist a wait and release this worker; the same stage is checked again later."""
        self.check_cancel()
        if not math.isfinite(delay_s) or delay_s <= 0:
            raise ValueError("defer delay_s must be finite and positive")
        raise JobDeferred(delay_s, reason, detail, detail_params)

    def sleep(self, seconds: float) -> None:
        """Interruptible wait for polling loops."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check_cancel()
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def wait_until(
        self,
        probe: Callable[[], Any],
        *,
        timeout_s: float,
        interval_s: float = 10,
        what: str = "condition",
    ) -> Any:
        """Poll `probe` until it returns a truthy value; raise on timeout."""
        deadline = time.monotonic() + timeout_s
        while True:
            self.check_cancel()
            result = probe()
            if result:
                return result
            if time.monotonic() > deadline:
                raise AppError(
                    "job.timeout", f"timed out after {int(timeout_s)}s waiting for {what}"
                )
            self.sleep(interval_s)


class JobEngine:
    def __init__(self, max_workers: int | None = None):
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers or get_settings().max_concurrent_jobs,
            thread_name_prefix="job",
        )
        self._cancel: set[str] = set()
        self._lock = threading.RLock()
        self._submitted: set[str] = set()
        self._resubmit: set[str] = set()
        self._reconcilers: list[tuple[str, Callable[[], None], float]] = []
        self._stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None
        self._scheduler_thread: threading.Thread | None = None

    # ---- lifecycle ----
    def start(
        self,
        job_type: str,
        target_id: str | None = None,
        payload: dict | None = None,
        *,
        run_inline: bool = False,
    ) -> str:
        stages = pipeline(job_type)
        job_id = new_id("job")
        with session_scope() as s:
            s.add(
                Job(
                    id=job_id,
                    type=job_type,
                    target_id=target_id,
                    status="queued",
                    payload=payload or {},
                    context={},
                    stages=[_pending_stage(st.name) for st in stages],
                )
            )
        append_log(job_id, f"queued {job_type} target={target_id}")
        self._submit(job_id, run_inline=run_inline)
        return job_id

    def retry(self, job_id: str) -> None:
        with self._lock:
            with session_scope() as s:
                job = s.get(Job, job_id)
                if not job:
                    raise NotFound("job.not_found", f"job {job_id} not found")
                if job.status in ACTIVE:
                    raise AppError("job.active", "job is still running", status=409)
                job.status = "queued"
                job.error = None
                job.error_code = None
                job.ended_at = None
                job.next_attempt_at = None
            self._cancel.discard(job_id)
            append_log(job_id, "retry requested")
            self._submit(job_id)

    def cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancel.add(job_id)
            with session_scope() as s:
                job = s.get(Job, job_id)
                idle = job is not None and job.status in {"queued", "waiting"}
                if idle:
                    job.stages = [
                        {
                            **st,
                            "status": "cancelled",
                            "ended_at": utcnow().isoformat(),
                            "next_attempt_at": None,
                        }
                        if st["status"] == "waiting"
                        else st
                        for st in job.stages
                    ]
            if idle:
                self._finish(job_id, "cancelled")
        append_log(job_id, "cancel requested")

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancel

    def resume_pending(self) -> list[str]:
        """On startup, recover interrupted jobs without bringing future waits forward."""
        with self._lock:
            if self._stop.is_set():
                return []
            with session_scope() as s:
                jobs = s.query(Job).filter(Job.status.in_(["queued", "running"])).all()
                ids = [job.id for job in jobs if job.id not in self._submitted]
                for job in jobs:
                    if job.id in ids:
                        job.status = "queued"
                        job.next_attempt_at = None
            for jid in ids:
                append_log(jid, "backend restarted — resuming from first unfinished stage")
                self._submit(jid)
            return ids + self.resume_due()

    def resume_due(self) -> list[str]:
        """Atomically claim due waits; competing schedulers cannot submit the same attempt."""
        with self._lock:
            if self._stop.is_set():
                return []
            due = or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= utcnow())
            with session_scope() as s:
                ids = [
                    row.id
                    for row in s.query(Job.id)
                    .filter(Job.status == "waiting", due)
                    .order_by(Job.next_attempt_at)
                    .all()
                ]
            claimed = []
            for jid in ids:
                with session_scope() as s:
                    changed = (
                        s.query(Job)
                        .filter(Job.id == jid, Job.status == "waiting", due)
                        .update(
                            {Job.status: "queued", Job.next_attempt_at: None},
                            synchronize_session=False,
                        )
                    )
                if changed:
                    append_log(jid, "scheduled check — resuming from first unfinished stage")
                    self._submit(jid)
                    claimed.append(jid)
            return claimed

    def start_scheduler(self, *, interval_s: float = 2) -> None:
        """Checks only the ledger, separately from potentially slow resource reconcilers."""
        with self._lock:
            if self._scheduler_thread or self._stop.is_set():
                return

            def loop() -> None:
                while not self._stop.is_set():
                    try:
                        self.resume_due()
                    except Exception:
                        log.exception("deferred job scheduling failed")
                    self._stop.wait(interval_s)

            self._scheduler_thread = threading.Thread(
                target=loop, name="job-scheduler", daemon=True
            )
            self._scheduler_thread.start()

    def shutdown(self) -> None:
        with self._lock:
            self._stop.set()
            self._pool.shutdown(wait=False, cancel_futures=True)
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)

    # ---- reconcilers ----
    def add_reconciler(self, name: str, fn: Callable[[], None], interval_s: float) -> None:
        self._reconcilers.append((name, fn, interval_s))

    def start_reconcilers(self) -> None:
        if self._reconcile_thread or not self._reconcilers:
            return
        last: dict[str, float] = {}

        def loop() -> None:
            while not self._stop.is_set():
                now = time.monotonic()
                for name, fn, interval in self._reconcilers:
                    if now - last.get(name, 0) >= interval:
                        last[name] = now
                        try:
                            fn()
                        except Exception:  # never let one reconciler kill the loop
                            log.exception("reconciler %s failed", name)
                self._stop.wait(2)

        self._reconcile_thread = threading.Thread(target=loop, name="reconcile", daemon=True)
        self._reconcile_thread.start()

    # ---- internals ----
    def _submit(self, job_id: str, *, run_inline: bool = False) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if job_id in self._submitted:
                # A fast retry/check can arrive after the durable status changed but before
                # the previous worker returned. Dispatch it as soon as that worker exits.
                self._resubmit.add(job_id)
                return
            self._submitted.add(job_id)
            if not run_inline:
                self._pool.submit(self._execute, job_id)
                return
        self._execute(job_id)

    def _execute(self, job_id: str) -> None:
        try:
            self._run(job_id)
        finally:
            with self._lock:
                self._submitted.discard(job_id)
                if job_id in self._resubmit:
                    self._resubmit.discard(job_id)
                    self._submit(job_id)

    def _update_stage(self, job_id: str, name: str, **fields: Any) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if not job:
                return
            stages = [dict(st) for st in job.stages]
            for st in stages:
                if st["name"] == name:
                    st.update(fields)
            job.stages = stages

    def _finish(
        self, job_id: str, status: str, error: str | None = None, code: str | None = None
    ) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if not job:
                return
            job_type, target = job.type, job.target_id
        # the hook updates the target resource first, so whoever sees the job terminal also
        # sees the resource's final state (hooks are idempotent if a crash re-runs them)
        hook = _HOOKS.get(job_type)
        if hook:
            try:
                hook(target or "", status, error)
            except Exception:
                log.exception("on_finish hook failed for %s", job_id)
        with session_scope() as s:
            job = s.get(Job, job_id)
            if not job:
                return
            job.status = status
            job.error = error
            job.error_code = code
            job.ended_at = utcnow()
            job.next_attempt_at = None
        with self._lock:
            self._cancel.discard(job_id)
        append_log(job_id, f"job {status}" + (f": {error}" if error else ""))

    def _defer(self, job_id: str, name: str, deferred: JobDeferred) -> None:
        with self._lock:
            if job_id in self._cancel:
                raise JobCancelled()
            next_attempt = utcnow() + timedelta(seconds=deferred.delay_s)
            with session_scope() as s:
                job = s.get(Job, job_id)
                if not job or job.status == "cancelled":
                    raise JobCancelled()
                job.status = "waiting"
                job.next_attempt_at = next_attempt
                job.stages = [
                    {
                        **st,
                        "status": "waiting",
                        "reason": deferred.reason,
                        "detail": deferred.detail,
                        "detail_params": deferred.detail_params,
                        "next_attempt_at": next_attempt.isoformat(),
                        "ended_at": None,
                    }
                    if st["name"] == name
                    else st
                    for st in job.stages
                ]
            append_log(
                job_id,
                f"[{name}] waiting: {deferred.reason}; next check {next_attempt.isoformat()}",
            )

    def _run(self, job_id: str) -> None:
        with self._lock, session_scope() as s:
            if self._stop.is_set():
                return
            claimed = (
                s.query(Job)
                .filter(Job.id == job_id, Job.status == "queued")
                .update(
                    {
                        Job.status: "running",
                        Job.started_at: func.coalesce(Job.started_at, utcnow()),
                        Job.next_attempt_at: None,
                    },
                    synchronize_session=False,
                )
            )
            if not claimed:
                return
            job = s.get(Job, job_id)
            job_type, target, payload = job.type, job.target_id, dict(job.payload or {})
            context = dict(job.context or {})
        ctx = StageContext(self, job_id, job_type, target, payload, context)
        try:
            registered = pipeline(job_type)
            with session_scope() as s:
                job = s.get(Job, job_id)
                previous = {st["name"]: st for st in job.stages}
                names = {stage.name for stage in registered}
                job.stages = [
                    previous.get(stage.name, _pending_stage(stage.name)) for stage in registered
                ] + [st for name, st in previous.items() if name not in names]
                done = {st["name"] for st in job.stages if st["status"] == "succeeded"}
            for stage in registered:
                if stage.name in done:
                    continue
                ctx.check_cancel()
                ctx.stage = stage.name
                prior = previous.get(stage.name, {})
                started_at = (
                    prior.get("started_at")
                    if prior.get("status") in {"running", "waiting"}
                    else None
                )
                self._update_stage(
                    job_id,
                    stage.name,
                    status="running",
                    started_at=started_at or utcnow().isoformat(),
                    ended_at=None,
                    reason=None,
                    detail_params=None,
                    next_attempt_at=None,
                )
                append_log(job_id, f"[{stage.name}] start")
                try:
                    try:
                        stage.fn(ctx)
                        ctx.check_cancel()
                    except JobDeferred as deferred:
                        self._defer(job_id, stage.name, deferred)
                        return
                except JobInterrupted:
                    raise
                except JobCancelled:
                    self._update_stage(
                        job_id, stage.name, status="cancelled", ended_at=utcnow().isoformat()
                    )
                    raise
                except Exception:
                    self._update_stage(
                        job_id, stage.name, status="failed", ended_at=utcnow().isoformat()
                    )
                    raise
                self._update_stage(
                    job_id, stage.name, status="succeeded", ended_at=utcnow().isoformat()
                )
                append_log(job_id, f"[{stage.name}] done")
            self._finish(job_id, "succeeded")
        except JobInterrupted:
            append_log(job_id, "backend stopping — job will resume after restart")
        except JobCancelled:
            self._finish(job_id, "cancelled")
        except AppError as e:
            if e.detail:
                append_log(job_id, f"detail: {e.detail}")
            self._finish(job_id, "failed", e.message, e.code)
        except Exception as e:  # unexpected: keep the traceback in the job log
            append_log(job_id, traceback.format_exc())
            code = "aws.client_error" if type(e).__name__ == "ClientError" else "internal"
            self._finish(job_id, "failed", f"{type(e).__name__}: {e}", code)


_engine: JobEngine | None = None


def get_engine() -> JobEngine:
    global _engine
    if _engine is None:
        _engine = JobEngine()
    return _engine


def set_engine(engine: JobEngine | None) -> None:
    global _engine
    _engine = engine


def job_view(job: Job) -> dict[str, Any]:
    next_attempt = job.next_attempt_at
    if next_attempt and next_attempt.tzinfo is None:
        # SQLite stores UTC datetimes without an offset; don't send local-looking dates.
        next_attempt = next_attempt.replace(tzinfo=UTC)
    return {
        "id": job.id,
        "type": job.type,
        "target_id": job.target_id,
        "status": job.status,
        "stages": job.stages,
        "error": job.error,
        "error_code": job.error_code,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "next_attempt_at": next_attempt,
    }


def latest_job(target_id: str, job_type: str | None = None) -> Job | None:
    with session_scope() as s:
        q = s.query(Job).filter(Job.target_id == target_id)
        if job_type:
            q = q.filter(Job.type == job_type)
        return q.order_by(Job.created_at.desc()).first()
