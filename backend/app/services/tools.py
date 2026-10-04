"""Pinned CLI tools the platform shells out to (helm for chart installs, kubectl for
operator debugging). Missing tools are downloaded into data/bin from the official
release hosts and verified against the published sha256 before use."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from ..core.config import get_settings
from ..core.errors import AppError

KUBECTL_VERSION = "v1.33.13"
HELM_VERSION = "v3.19.0"


def _arch() -> str:
    m = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[m]


def bin_dir() -> Path:
    d = get_settings().data_dir / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _which(name: str) -> str | None:
    local = bin_dir() / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name)


def _version(path: str, args: list[str]) -> str | None:
    try:
        out = subprocess.run([path, *args], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0][:120]
    except Exception:
        return None


def tool_status() -> dict[str, dict[str, str | None]]:
    helm = _which("helm")
    kubectl = _which("kubectl")
    return {
        "helm": {"path": helm, "version": _version(helm, ["version", "--short"]) if helm else None},
        "kubectl": {
            "path": kubectl,
            "version": _version(kubectl, ["version", "--client"]) if kubectl else None,
        },
    }


def _fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise AppError("tools.insecure_url", f"refusing non-https download {url}")
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - https only
        return r.read()


def _verify(data: bytes, expected_hex: str, what: str) -> None:
    got = hashlib.sha256(data).hexdigest()
    if got != expected_hex.strip().split()[0]:
        raise AppError("tools.checksum_mismatch", f"{what}: sha256 mismatch")


def ensure_kubectl() -> str:
    found = _which("kubectl")
    if found:
        return found
    base = f"https://dl.k8s.io/release/{KUBECTL_VERSION}/bin/linux/{_arch()}/kubectl"
    data = _fetch(base)
    _verify(data, _fetch(base + ".sha256").decode(), "kubectl")
    dest = bin_dir() / "kubectl"
    dest.write_bytes(data)
    dest.chmod(0o755)
    return str(dest)


def ensure_helm() -> str:
    found = _which("helm")
    if found:
        return found
    name = f"helm-{HELM_VERSION}-linux-{_arch()}.tar.gz"
    url = f"https://get.helm.sh/{name}"
    data = _fetch(url)
    _verify(data, _fetch(url + ".sha256sum").decode(), "helm")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        member = tar.getmember(f"linux-{_arch()}/helm")
        f = tar.extractfile(member)
        assert f is not None
        dest = bin_dir() / "helm"
        dest.write_bytes(f.read())
    dest.chmod(0o755)
    return str(dest)
