"""The only place Kubernetes API clients are created (one per EKS cluster).

Auth is the standard EKS bearer token: a presigned STS GetCallerIdentity URL
carrying the `x-k8s-aws-id` header, base64url-encoded with the `k8s-aws-v1.`
prefix (what `aws eks get-token` emits). Tokens live 15 min; we refresh at 10.
"""

from __future__ import annotations

import base64
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from botocore.signers import RequestSigner
from kubernetes import client as k8s

from . import aws

TOKEN_TTL_S = 600

ApiFactory = Callable[[str, str], Any]  # (region, eks_name) -> kubernetes.client.ApiClient

_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[Any, float]] = {}
_factory: ApiFactory | None = None


def eks_token(region: str, eks_name: str) -> str:
    sts = aws.client("sts", region)
    signer = RequestSigner(
        sts.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        sts._request_signer._credentials,  # noqa: SLF001 - same creds the client uses
        sts.meta.events,
    )
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": eks_name},
            "context": {},
        },
        region_name=region,
        expires_in=60,
        operation_name="",
    )
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _default_factory(region: str, eks_name: str) -> Any:
    desc = aws.client("eks", region).describe_cluster(name=eks_name)["cluster"]
    ca = tempfile.NamedTemporaryFile(prefix="eks-ca-", suffix=".crt", delete=False)
    ca.write(base64.b64decode(desc["certificateAuthority"]["data"]))
    ca.close()
    cfg = k8s.Configuration()
    cfg.host = desc["endpoint"]
    cfg.ssl_ca_cert = ca.name
    # kubernetes>=35 resolves the prefix under "BearerToken" (alias "authorization");
    # a hook refreshes the 15-minute EKS token transparently before it expires.
    state = {"at": 0.0}

    def refresh(conf):
        if time.monotonic() - state["at"] > TOKEN_TTL_S:
            conf.api_key = {"BearerToken": eks_token(region, eks_name)}
            conf.api_key_prefix = {"BearerToken": "Bearer"}
            state["at"] = time.monotonic()

    cfg.refresh_api_key_hook = refresh
    refresh(cfg)
    return k8s.ApiClient(cfg)


def api_client(region: str, eks_name: str) -> Any:
    key = (region, eks_name)
    with _lock:
        hit = _cache.get(key)
        if hit:
            return hit[0]
        api = (_factory or _default_factory)(region, eks_name)
        _cache[key] = (api, time.monotonic())
        return api


def core(region: str, eks_name: str) -> Any:
    return k8s.CoreV1Api(api_client(region, eks_name))


def apps(region: str, eks_name: str) -> Any:
    return k8s.AppsV1Api(api_client(region, eks_name))


def batch(region: str, eks_name: str) -> Any:
    return k8s.BatchV1Api(api_client(region, eks_name))


def custom(region: str, eks_name: str) -> Any:
    return k8s.CustomObjectsApi(api_client(region, eks_name))


def set_factory(factory: ApiFactory | None) -> None:
    global _factory
    with _lock:
        _factory = factory
        _cache.clear()
