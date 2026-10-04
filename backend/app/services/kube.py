"""Kubernetes helpers: idempotent server-side apply, kubeconfig for helm, waits.

All manifests TuningPad owns are applied with field manager `tuningpad` and
labelled `app.kubernetes.io/managed-by=tuningpad`.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import yaml
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError, ResourceNotFoundError

from ..core import aws, k8s
from ..core.errors import AppError

NAMESPACE = "tuningpad"
FIELD_MANAGER = "tuningpad"
MANAGED_LABEL = {"app.kubernetes.io/managed-by": "tuningpad"}


def dynamic(region: str, eks_name: str) -> DynamicClient:
    return DynamicClient(k8s.api_client(region, eks_name))


def _labelled(manifest: dict[str, Any]) -> dict[str, Any]:
    meta = manifest.setdefault("metadata", {})
    meta["labels"] = {**MANAGED_LABEL, **(meta.get("labels") or {})}
    return manifest


def apply(region: str, eks_name: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Server-side apply one object (create or converge)."""
    dyn = dynamic(region, eks_name)
    m = _labelled(manifest)
    try:
        res = dyn.resources.get(api_version=m["apiVersion"], kind=m["kind"])
    except ResourceNotFoundError as e:
        raise AppError(
            "k8s.kind_missing",
            f"{m['apiVersion']}/{m['kind']} is not installed in the cluster (CRD missing?)",
        ) from e
    ns = m["metadata"].get("namespace") if res.namespaced else None
    out = res.server_side_apply(
        body=m,
        name=m["metadata"]["name"],
        namespace=ns,
        field_manager=FIELD_MANAGER,
        force_conflicts=True,
    )
    return out.to_dict()


def get(
    region: str,
    eks_name: str,
    api_version: str,
    kind: str,
    name: str,
    namespace: str | None = NAMESPACE,
) -> dict[str, Any] | None:
    dyn = dynamic(region, eks_name)
    res = dyn.resources.get(api_version=api_version, kind=kind)
    try:
        return res.get(name=name, namespace=namespace if res.namespaced else None).to_dict()
    except (NotFoundError, ApiException) as e:
        if isinstance(e, NotFoundError) or getattr(e, "status", None) == 404:
            return None
        raise


def delete(
    region: str,
    eks_name: str,
    api_version: str,
    kind: str,
    name: str,
    namespace: str | None = NAMESPACE,
) -> bool:
    dyn = dynamic(region, eks_name)
    res = dyn.resources.get(api_version=api_version, kind=kind)
    try:
        res.delete(
            name=name,
            namespace=namespace if res.namespaced else None,
            body={"propagationPolicy": "Foreground"},
        )
        return True
    except NotFoundError:
        return False


def list_objects(
    region: str,
    eks_name: str,
    api_version: str,
    kind: str,
    namespace: str | None = NAMESPACE,
    label_selector: str | None = None,
) -> list[dict]:
    dyn = dynamic(region, eks_name)
    res = dyn.resources.get(api_version=api_version, kind=kind)
    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if res.namespaced and namespace:
        kwargs["namespace"] = namespace
    return [i.to_dict() for i in res.get(**kwargs).items]


def pod_log(
    region: str,
    eks_name: str,
    pod: str,
    namespace: str = NAMESPACE,
    since_bytes: int | None = None,
    tail_lines: int | None = None,
) -> str:
    core = k8s.core(region, eks_name)
    kwargs: dict[str, Any] = {}
    if tail_lines:
        kwargs["tail_lines"] = tail_lines
    if since_bytes:
        kwargs["limit_bytes"] = since_bytes
    return core.read_namespaced_pod_log(pod, namespace, **kwargs)


@contextmanager
def kubeconfig(region: str, eks_name: str) -> Iterator[str]:
    """Temporary kubeconfig (static bearer token) for helm/kubectl subprocesses."""
    desc = aws.client("eks", region).describe_cluster(name=eks_name)["cluster"]
    cfg = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": eks_name,
                "cluster": {
                    "server": desc["endpoint"],
                    "certificate-authority-data": desc["certificateAuthority"]["data"],
                },
            }
        ],
        "users": [{"name": "tuningpad", "user": {"token": k8s.eks_token(region, eks_name)}}],
        "contexts": [{"name": "tp", "context": {"cluster": eks_name, "user": "tuningpad"}}],
        "current-context": "tp",
    }
    fd, path = tempfile.mkstemp(prefix="kubeconfig-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(cfg, f)
        os.chmod(path, 0o600)
        yield path
    finally:
        os.unlink(path)


def helm(region: str, eks_name: str, args: list[str], timeout: int = 900) -> str:
    from .tools import ensure_helm

    helm_bin = ensure_helm()
    with kubeconfig(region, eks_name) as kc:
        env = {
            **os.environ,
            "KUBECONFIG": kc,
            "HELM_CACHE_HOME": tempfile.gettempdir() + "/tp-helm",
        }

        def run(argv: list[str], t: int = timeout) -> subprocess.CompletedProcess:
            return subprocess.run(
                [helm_bin, *argv], capture_output=True, text=True, env=env, timeout=t
            )

        if args[:2] == ["upgrade", "--install"]:
            _clear_stuck_release(run, args)
        proc = run(args)
    if proc.returncode != 0:
        raise AppError(
            "helm.failed",
            f"helm {' '.join(args[:3])} failed",
            detail=(proc.stderr or proc.stdout)[-4000:],
        )
    return proc.stdout


def _clear_stuck_release(run, args: list[str]) -> None:
    """A helm process killed mid-operation (backend restart during `--wait`) leaves the
    release `pending-*`, and every later `upgrade --install` fails with "another operation
    is in progress" / "release: already exists". Undo the interrupted operation first:
    a pending first install is uninstalled, a pending upgrade/rollback is rolled back."""
    release = args[2]
    ns = args[args.index("--namespace") + 1] if "--namespace" in args else "default"
    st = run(["status", release, "--namespace", ns, "-o", "json"], 60)
    if st.returncode != 0:
        return  # no such release
    try:
        info = json.loads(st.stdout)
    except ValueError:
        return
    status = (info.get("info") or {}).get("status", "")
    if not status.startswith("pending"):
        return
    if status == "pending-install" or int(info.get("version") or 1) <= 1:
        fix = ["uninstall", release, "--namespace", ns, "--wait"]
    else:
        fix = ["rollback", release, "--namespace", ns, "--wait"]
    proc = run(fix, 600)
    if proc.returncode != 0:
        raise AppError(
            "helm.failed",
            f"helm release {release} is stuck in {status} and could not be cleared",
            detail=(proc.stderr or proc.stdout)[-4000:],
        )


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()
