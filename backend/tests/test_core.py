import pytest

from app.core.auth import validate_startup
from app.core.config import Settings, reset_settings_cache


def test_health_and_loopback_without_password(client):
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/auth/status").json() == {"auth_required": False, "authenticated": True}


def test_password_flow(monkeypatch):
    monkeypatch.setenv("TUNINGPAD_PASSWORD", "s3cret")
    reset_settings_cache()
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(start_background=False)) as c:
        r = c.get("/api/meta")
        assert r.status_code == 401 and r.json()["code"] == "auth.required"
        assert (
            c.post("/api/auth/login", json={"password": "nope"}).json()["code"]
            == "auth.invalid_password"
        )
        assert c.post("/api/auth/login", json={"password": "s3cret"}).status_code == 200
        assert c.get("/api/meta").status_code == 200
        c.post("/api/auth/logout")
        assert c.get("/api/meta").status_code == 401


def test_non_loopback_refused_without_password(client):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(start_background=False), client=("10.0.0.5", 1234)) as c:
        r = c.get("/api/meta")
        assert r.status_code == 403 and r.json()["code"] == "auth.loopback_only"


def test_startup_guard():
    with pytest.raises(RuntimeError):
        validate_startup(Settings(host="0.0.0.0", password=""))
    with pytest.raises(RuntimeError):
        validate_startup(Settings(run_mode="prod", password=""))
    validate_startup(Settings(host="0.0.0.0", password="x"))


def test_error_envelope(client):
    r = client.get("/api/jobs/missing")
    assert r.status_code == 404
    assert r.json()["code"] == "job.not_found"
    r = client.get("/api/nowhere")
    assert r.json()["code"] == "http.404"


def test_k8s_bearer_prefix_and_refresh(monkeypatch):
    import base64

    from kubernetes import client as kc

    from app.core import k8s

    tokens = iter(["t1", "t2"])
    monkeypatch.setattr(k8s, "eks_token", lambda r, n: next(tokens))

    class EKS:
        def describe_cluster(self, name):
            return {
                "cluster": {
                    "endpoint": "https://x",
                    "certificateAuthority": {"data": base64.b64encode(b"ca").decode()},
                }
            }

    monkeypatch.setattr(k8s.aws, "client", lambda svc, region=None: EKS())
    api = k8s._default_factory("us-east-1", "eks")
    cfg: kc.Configuration = api.configuration
    assert cfg.auth_settings()["BearerToken"]["value"] == "Bearer t1"
    monkeypatch.setattr(k8s, "TOKEN_TTL_S", -1)
    assert cfg.auth_settings()["BearerToken"]["value"] == "Bearer t2"


# ---------------- helm: stuck releases ----------------


class _Proc:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


@pytest.mark.parametrize(
    "status,version,fix",
    [
        ("pending-install", 1, "uninstall"),
        ("pending-upgrade", 3, "rollback"),
        ("deployed", 2, None),
        (None, 0, None),  # release absent
    ],
)
def test_helm_clears_release_left_pending_by_interrupted_run(status, version, fix):
    import json as _json

    from app.services import kube

    calls = []

    def run(argv, t=0):
        calls.append(argv[0])
        if argv[0] == "status":
            if status is None:
                return _Proc(1)
            return _Proc(0, _json.dumps({"info": {"status": status}, "version": version}))
        return _Proc(0)

    args = ["upgrade", "--install", "kuberay-operator", "c", "--namespace", "kuberay"]
    kube._clear_stuck_release(run, args)
    assert calls == ["status"] + ([fix] if fix else [])
