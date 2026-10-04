"""The only place boto3 clients are created.

Everything else calls `aws.client("eks", region)`; tests install a stub factory
with `set_factory()` so no code path can reach AWS from the test suite.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config

_BOTO_CONFIG = Config(retries={"max_attempts": 8, "mode": "adaptive"}, user_agent_extra="tuningpad")

ClientFactory = Callable[[str, str | None], Any]

_lock = threading.Lock()
_cache: dict[tuple[str, str | None], Any] = {}
_factory: ClientFactory | None = None
_session: boto3.Session | None = None


def _default_factory(service: str, region: str | None) -> Any:
    global _session
    if _session is None:
        _session = boto3.Session()
    return _session.client(service, region_name=region, config=_BOTO_CONFIG)


def client(service: str, region: str | None = None) -> Any:
    key = (service, region)
    with _lock:
        if key not in _cache:
            _cache[key] = (_factory or _default_factory)(service, region)
        return _cache[key]


def set_factory(factory: ClientFactory | None) -> None:
    """Install a stub factory (tests) or restore the default (None)."""
    global _factory
    with _lock:
        _factory = factory
        _cache.clear()


def credentials_frozen():
    """Resolved credentials for presigning (EKS tokens). Default factory only."""
    global _session
    if _session is None:
        _session = boto3.Session()
    creds = _session.get_credentials()
    return creds.get_frozen_credentials() if creds else None


def tags(extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Standard resource tags in the [{Key, Value}] shape most APIs take."""
    from .config import get_settings

    base = {"Project": get_settings().resource_tag, "ManagedBy": "tuningpad"}
    base.update(extra or {})
    return [{"Key": k, "Value": v} for k, v in base.items()]


def tag_map(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in tags(extra)}
