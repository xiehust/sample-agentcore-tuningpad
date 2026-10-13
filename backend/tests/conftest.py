"""Hermetic test harness: temp data dir + SQLite, no real AWS / Kubernetes."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import aws, k8s
from app.core.config import get_settings, reset_settings_cache
from app.services import toolkit_source


class _NoAws:
    def __call__(self, service, region):
        raise AssertionError(f"unexpected real AWS client: {service}@{region} — install a stub")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.upper().startswith("TUNINGPAD_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TUNINGPAD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TUNINGPAD_CONFIG_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    reset_settings_cache()

    # Guard subprocess acquisition/build, not the validator: tests exercise real Git checks.
    # Only explicit file-protocol fetches from local test repositories may run.
    popen = subprocess.Popen

    def guarded_popen(args, *a, **kw):
        if isinstance(args, list | tuple) and args:
            exe = Path(args[0]).name
            if exe == "uv" and "build" in args:
                raise AssertionError("unexpected uv build — install a stub")
            if exe == "git" and ("fetch" in args or "clone" in args):
                local_fetch = (
                    "fetch" in args
                    and "protocol.file.allow=always" in args
                    and "--" in args
                    and Path(args[args.index("--") + 1]).is_absolute()
                )
                if not local_fetch:
                    raise AssertionError("unexpected real Git fetch/clone — install a stub")
        return popen(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
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


@pytest.fixture
def toolkit_installation(monkeypatch):
    """Stub installer metadata, not validation; callers can mutate it to test rejection."""
    url, revision = toolkit_source.source_spec()
    direct = {"url": url, "vcs_info": {"vcs": "git", "commit_id": revision}}
    original = toolkit_source.metadata.distribution

    def distribution(name):
        if name == "agentcore-rl-toolkit":
            return SimpleNamespace(read_text=lambda file: json.dumps(direct))
        return original(name)

    monkeypatch.setattr(toolkit_source.metadata, "distribution", distribution)
    return direct


@pytest.fixture
def toolkit_repo(tmp_path, monkeypatch, toolkit_installation):
    """Synthetic tracked consumer inputs; no sibling checkout or upstream safety claim."""
    from app.templates_lib import get_template

    root = tmp_path / "fixture-toolkit"
    root.mkdir()
    files = {
        "pyproject.toml": '[project]\nname = "agentcore-rl-toolkit"\nversion = "0.1.5"\n',
        "README.md": "fixture source\n",
        "uv.lock": "fixture lock\n",
        "src/agentcore_rl_toolkit/__init__.py": "# fixture package\n",
        "patches/apply-megatron-bridge-cp-clamp.sh": "# fixture patch script\n",
        "patches/megatron-bridge-cp-chunk1-clamp.diff": "fixture patch\n",
        ".gitignore": "dist/\n__pycache__/\nignored*\n",
        "examples/strands_officebench_agent/preprocess.py": (
            "from pathlib import Path\n"
            "def upload_task(s3, task_dir, task_id, bucket, prefix):\n"
            "    # Read the helper at call time: its private snapshot must still exist.\n"
            "    s3.put_object(Bucket=bucket, Key=f'{prefix}/{task_id}/helper',\n"
            "                  Body=Path(__file__).read_bytes())\n"
            "    return len(list((Path(task_dir) / 'subtasks').glob('*.json')))\n"
        ),
    }
    for tid in ("gsm8k_math", "officebench"):
        template = get_template(tid)
        for name in template["toolkit_files"]:
            files[f"{template['toolkit_dir']}/{name}"] = f"# fixture {tid}/{name}\n"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / "README-link.md").symlink_to("README.md")

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid", *args],
            cwd=root,
            env=toolkit_source._git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    git("init", "--template=")
    git("remote", "add", "origin", toolkit_installation["url"])
    git("add", "-A")
    git("commit", "-m", "consumer fixture")
    revision = git("rev-parse", "HEAD")
    declaration = tmp_path / "source.toml"
    declaration.write_text(
        "[tool.uv.sources]\nagentcore-rl-toolkit = "
        f'{{ git = "{toolkit_installation["url"]}", rev = "{revision}" }}\n'
    )
    monkeypatch.setattr(toolkit_source, "_PYPROJECT", declaration)
    toolkit_installation["vcs_info"]["commit_id"] = revision
    cache = get_settings().data_dir / "cache" / "toolkit" / revision
    cache.parent.mkdir(parents=True)
    root.rename(cache)
    root = cache
    (root / "ignored-secret").write_text("must not be packaged")
    return SimpleNamespace(root=root, revision=revision, files=files, git=git)


@pytest.fixture
def stub_toolkit_build(monkeypatch):
    calls = []
    run = subprocess.run

    def build(args, **kwargs):
        if args[:2] != ["uv", "build"]:
            return run(args, **kwargs)
        root = Path(kwargs["cwd"])
        assert not (root / ".git").exists()
        assert not (root / "ignored-secret").exists()
        calls.append((args, root))
        dist = Path(args[args.index("-o") + 1])
        assert dist.is_absolute()
        wheel = dist / "agentcore_rl_toolkit-0.1.5-py3-none-any.whl"
        wheel.write_bytes((root / "README.md").read_bytes())
        (root / "build-residue").write_text("private build output")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", build)
    return calls
