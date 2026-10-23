"""Reuse the durable deletion intent when a confirmed request is repeated."""

from __future__ import annotations

import threading

from ..core.db import session_scope
from ..core.errors import AppError, NotFound
from ..jobs.engine import ACTIVE, get_engine, latest_job
from ..models import Cluster

# The control plane runs one job engine per process. Serialize the brief ledger/start
# handoff so double clicks cannot create competing pipelines for the same resources.
_request_lock = threading.Lock()


def request_delete(cluster_id: str, confirm: str) -> str:
    with _request_lock:
        with session_scope() as s:
            cluster = s.get(Cluster, cluster_id)
            if not cluster:
                raise NotFound("cluster.not_found", f"cluster {cluster_id} not found")
            if confirm != cluster.name:
                raise AppError("cluster.confirm_name", "type the cluster name to confirm deletion")
            previous = latest_job(cluster_id, "cluster.delete")
            if previous and (
                previous.status in ACTIVE
                or (previous.status == "succeeded" and cluster.status == "deleted")
            ):
                return previous.id
            cluster.status = "deleting"

        engine = get_engine()
        if previous and previous.status in {"failed", "cancelled"}:
            # The context remembers successful DeleteAgentRuntime requests. Creating a
            # fresh job loses it and can turn an already-deleted runtime into AccessDenied.
            engine.retry(previous.id)
            return previous.id
        return engine.start("cluster.delete", cluster_id)
