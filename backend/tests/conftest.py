"""Hermetic test harness: temp data dir + SQLite, no real AWS / Kubernetes."""

from __future__ import annotations

import pytest

from app.core import aws, k8s
from app.core.config import reset_settings_cache


class _NoAws:
    def __call__(self, service, region):
        raise AssertionError(f"unexpected real AWS client: {service}@{region} — install a stub")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNINGPAD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TUNINGPAD_CONFIG_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.delenv("TUNINGPAD_PASSWORD", raising=False)
    reset_settings_cache()
    from app.core.db import init_db

    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    aws.set_factory(_NoAws())
    k8s.set_factory(lambda r, n: (_ for _ in ()).throw(AssertionError("unexpected k8s client")))
    from app.jobs import engine as eng

    eng.set_engine(eng.JobEngine(max_workers=2))
    yield
    eng.get_engine().shutdown()
    eng.set_engine(None)
    aws.set_factory(None)
    k8s.set_factory(None)
    reset_settings_cache()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(start_background=False)) as c:
        yield c


class StubClient:
    """Minimal boto3 client stub: map method name -> callable or return value."""

    def __init__(self, **methods):
        self.calls: list[tuple[str, dict]] = []
        self._methods = methods

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._methods:
            raise AssertionError(f"stub has no method {name}")
        impl = self._methods[name]

        def call(*args, **kwargs):
            if args:  # positional-style helpers such as s3.upload_file(path, bucket, key)
                kwargs = {"_args": args, **kwargs}
            self.calls.append((name, kwargs))
            return impl(**kwargs) if callable(impl) else impl

        return call


@pytest.fixture
def stub_aws():
    """Install per-service stubs: stub_aws({"sts": StubClient(...)})."""

    def install(services: dict[str, StubClient]):
        def factory(service, region):
            if service not in services:
                raise AssertionError(f"no stub for {service}")
            return services[service]

        aws.set_factory(factory)
        return services

    return install
