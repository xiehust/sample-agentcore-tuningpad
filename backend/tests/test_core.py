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
