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
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import get_settings
from ..core.db import new_id, session_scope, utcnow
from ..core.errors import AppError, NotFound
from ..models import Job

log = logging.getLogger(__name__)

TERMINAL = {"succeeded", "failed", "cancelled"}
ACTIVE = {"queued", "running"}
LOG_READ_LIMIT = 256 * 1024


class JobCancelled(Exception):
    pass


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
        self._lock = threading.Lock()
        self._reconcilers: list[tuple[str, Callable[[], None], float]] = []
        self._stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None

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
                    stages=[
                        {
                            "name": st.name,
                            "status": "pending",
                            "detail": None,
                            "started_at": None,
                            "ended_at": None,
                        }
                        for st in stages
                    ],
                )
            )
        append_log(job_id, f"queued {job_type} target={target_id}")
        if run_inline:
            self._run(job_id)
        else:
            self._pool.submit(self._run, job_id)
        return job_id

    def retry(self, job_id: str) -> None:
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
        append_log(job_id, "retry requested")
        self._pool.submit(self._run, job_id)

    def cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancel.add(job_id)
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job and job.status == "queued":
                job.status = "cancelled"
                job.ended_at = utcnow()
        append_log(job_id, "cancel requested")

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancel

    def resume_pending(self) -> list[str]:
        """On startup: re-run jobs that were queued/running when the process died."""
        with session_scope() as s:
            ids = [j.id for j in s.query(Job).filter(Job.status.in_(list(ACTIVE))).all()]
        for jid in ids:
            append_log(jid, "backend restarted — resuming from first unfinished stage")
            self._pool.submit(self._run, jid)
        return ids

    def shutdown(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

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
        with self._lock:
            self._cancel.discard(job_id)
        append_log(job_id, f"job {status}" + (f": {error}" if error else ""))

    def _run(self, job_id: str) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if not job or job.status in TERMINAL:
                return
            job.status = "running"
            job.started_at = job.started_at or utcnow()
            job_type, target, payload = job.type, job.target_id, dict(job.payload or {})
            context = dict(job.context or {})
            done = {st["name"] for st in job.stages if st["status"] == "succeeded"}
        ctx = StageContext(self, job_id, job_type, target, payload, context)
        try:
            for stage in pipeline(job_type):
                if stage.name in done:
                    continue
                ctx.check_cancel()
                ctx.stage = stage.name
                self._update_stage(
                    job_id,
                    stage.name,
                    status="running",
                    started_at=utcnow().isoformat(),
                    ended_at=None,
                )
                append_log(job_id, f"[{stage.name}] start")
                try:
                    stage.fn(ctx)
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
    }


def latest_job(target_id: str, job_type: str | None = None) -> Job | None:
    with session_scope() as s:
        q = s.query(Job).filter(Job.target_id == target_id)
        if job_type:
            q = q.filter(Job.type == job_type)
        return q.order_by(Job.created_at.desc()).first()
