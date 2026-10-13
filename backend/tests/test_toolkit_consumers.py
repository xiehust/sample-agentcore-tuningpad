"""Consumer wiring uses real resolver validation and disposable tracked Git inputs."""

import hashlib
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from app.catalog import models
from app.core.config import get_settings
from app.core.errors import AppError
from app.services import agents, datasets, project, toolkit_source, trainer_image
from app.templates_lib import get_template, template_dir
from tests.conftest import StubClient


def snapshot(root):
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def spy_source(monkeypatch):
    resolutions, copies = [], []
    resolve, copy = toolkit_source.resolve_source, toolkit_source.copy_tracked

    def resolve_spy(*args, **kwargs):
        result = resolve(*args, **kwargs)
        resolutions.append(result)
        return result

    def copy_spy(root, dest, **kwargs):
        result = copy(root, dest, **kwargs)
        copies.append((root, dest, kwargs["expected_revision"]))
        return result

    monkeypatch.setattr(toolkit_source, "resolve_source", resolve_spy)
    monkeypatch.setattr(toolkit_source, "copy_tracked", copy_spy)
    return resolutions, copies


@pytest.mark.parametrize(
    "source,tid", [("template", "gsm8k_math"), ("template", "officebench"), ("upload", None)]
)
def test_agent_resolves_once_and_builds_same_tracked_snapshot(
    source, tid, toolkit_repo, stub_toolkit_build, monkeypatch
):
    f = toolkit_repo
    before = snapshot(f.root)
    resolutions, copies = spy_source(monkeypatch)
    if source == "upload":
        with zipfile.ZipFile(agents.uploads_dir() / "ag-fixture.zip", "w") as z:
            z.writestr("Dockerfile", "uploaded Dockerfile")
            z.writestr("app.py", "# uploaded app")
            z.writestr("dist/stale.whl", "old wheel")
    ctx = agents.prepare_context(
        {"id": "ag-fixture", "source": source, "template_id": tid, "config": {}}
    )
    assert resolutions == [(f.root, f.revision)]
    assert len(copies) == len(stub_toolkit_build) == 1
    root, private, revision = copies[0]
    assert (root, revision) == (f.root, f.revision)
    assert stub_toolkit_build[0][1] == private
    assert private != f.root and not private.is_relative_to(ctx)
    assert not private.exists()  # cleaned after the wheel, never shipped with Docker context
    wheel = next((ctx / "dist").glob("*.whl"))
    assert wheel.read_text() == f.files["README.md"]
    assert len(list((ctx / "dist").glob("*.whl"))) == 1
    if tid:
        t = get_template(tid)
        overlay = template_dir(tid) / "agent"
        for name in t["toolkit_files"]:
            expected = (
                overlay / name if (overlay / name).exists() else f.root / t["toolkit_dir"] / name
            )
            assert (ctx / name).read_bytes() == expected.read_bytes()
        for path in overlay.iterdir():
            assert (ctx / path.name).read_bytes() == path.read_bytes()
        assert json.loads((ctx / "tp_config.json").read_text()) is not None
    else:
        assert (ctx / "Dockerfile").read_text() == "uploaded Dockerfile"
        assert (ctx / "app.py").read_text() == "# uploaded app"
    assert snapshot(f.root) == before


@pytest.mark.parametrize("failure", ["exit", "missing-wheel"])
def test_failed_wheel_build_cleans_private_copy_not_source(failure, toolkit_repo, monkeypatch):
    f = toolkit_repo
    before = snapshot(f.root)
    private_paths = []
    run = subprocess.run

    def fail_build(args, **kwargs):
        if args[:2] != ["uv", "build"]:
            return run(args, **kwargs)
        private = Path(kwargs["cwd"])
        private_paths.append(private)
        (private / "build-residue").write_text("failed build")
        return subprocess.CompletedProcess(
            args, 1 if failure == "exit" else 0, "", "fixture failure"
        )

    monkeypatch.setattr(subprocess, "run", fail_build)
    with pytest.raises(AppError) as caught:
        agents.prepare_context(
            {"id": "ag-fail", "source": "template", "template_id": "gsm8k_math", "config": {}}
        )
    assert caught.value.code == "agent.wheel_failed"
    assert len(private_paths) == 1 and not private_paths[0].exists()
    assert snapshot(f.root) == before


@pytest.mark.parametrize("consumer", ["agent", "helper"])
def test_source_consumers_reject_bad_installation_before_execution(
    consumer, toolkit_repo, toolkit_installation, stub_toolkit_build
):
    toolkit_installation["vcs_info"]["commit_id"] = "0" * 40
    with pytest.raises(AppError) as caught:
        if consumer == "agent":
            agents.prepare_context(
                {
                    "id": "ag-invalid",
                    "source": "template",
                    "template_id": "gsm8k_math",
                    "config": {},
                }
            )
        else:
            with datasets._toolkit_module(
                "examples/strands_officebench_agent/preprocess.py", "ob_preprocess"
            ):
                pytest.fail("invalid installation executed a source helper")
    assert caught.value.code == "toolkit.source_invalid"
    assert caught.value.detail == {"reason": "installed_direct_url"}
    assert not stub_toolkit_build
    assert not list(toolkit_repo.root.rglob("__pycache__"))


def test_officebench_helper_and_trainer_share_source_without_cache_writes(
    toolkit_repo, monkeypatch, tmp_path
):
    f = toolkit_repo
    before = snapshot(f.root)
    resolutions, copies = spy_source(monkeypatch)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    rel = "examples/strands_officebench_agent/preprocess.py"
    with datasets._toolkit_module(rel, "ob_preprocess") as pre:
        helper = Path(pre.__file__)
        assert helper.read_text() == f.files[rel]
        assert helper == copies[0][1] / rel
        assert list(helper.parent.glob("__pycache__/*.pyc"))
    assert not helper.exists()
    dest = trainer_image.package_source(tmp_path / "source.zip", expected_revision=f.revision)
    assert resolutions == [(f.root, f.revision)] * 2
    assert all(root == f.root and revision == f.revision for root, _, revision in copies)
    with zipfile.ZipFile(dest) as z:
        assert z.read(f"toolkit/{rel}").decode() == f.files[rel]
    assert all(not private.exists() for _, private, _ in copies)
    assert snapshot(f.root) == before


def test_officebench_keeps_data_checkout_upload_and_split_contract(
    toolkit_repo, tmp_path, monkeypatch, stub_aws
):
    f = toolkit_repo
    repo = tmp_path / "OfficeBench"
    for task_id in ("1-2", "1-10", "2-1", "2-2"):
        task = repo / "tasks" / task_id
        (task / "subtasks").mkdir(parents=True)
        for i in (1, 2):
            (task / "subtasks" / f"{i}.json").write_text(json.dumps({"task": f"{task_id}/{i}"}))
        if task_id.startswith("1-"):
            (task / "testbed").mkdir()
            (task / "testbed" / "file").write_text("testbed")
    events = []
    monkeypatch.setattr(datasets, "officebench_checkout", lambda: events.append("data") or repo)
    resolve = toolkit_source.resolve_source
    monkeypatch.setattr(
        toolkit_source, "resolve_source", lambda: events.append("toolkit") or resolve()
    )
    s3 = StubClient(put_object={})
    stub_aws({"s3": s3})
    out = datasets.officebench_rows(
        "ds-1", "us-east-1", "bucket", lambda msg: None, val_fraction=0.5, seed=3
    )
    assert events == ["data", "toolkit"]
    assert len(out["train"]) == len(out["val"]) == 4
    train_ids = {r["prompt"][0]["content"].split("/")[0] for r in out["train"]}
    val_ids = {r["prompt"][0]["content"].split("/")[0] for r in out["val"]}
    assert not train_ids & val_ids
    assert {t.split("-")[0] for t in train_ids} == {"1", "2"}
    for rows in out.values():
        for row in rows:
            task_id, subtask = row["prompt"][0]["content"].split("/")
            prefix = f"s3://bucket/datasets/ds-1/officebench/{task_id}"
            assert row["payload"] == {
                "task_uri": f"{prefix}/{subtask}/config.json",
                "testbed_uri": f"{prefix}/testbed.tar.gz" if task_id.startswith("1-") else None,
            }
    assert [kw["Key"] for _, kw in s3.calls] == [
        f"datasets/ds-1/officebench/{tid}/helper" for tid in ("1-2", "1-10", "2-1", "2-2")
    ]
    assert all(
        kw["Body"].decode() == f.files["examples/strands_officebench_agent/preprocess.py"]
        for _, kw in s3.calls
    )
    assert not list(f.root.rglob("__pycache__"))


def test_gsm8k_and_upload_do_not_resolve_toolkit(tmp_path, monkeypatch):
    import huggingface_hub

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected toolkit resolution")

    monkeypatch.setattr(toolkit_source, "resolve_source", forbidden)
    parquet = tmp_path / "gsm8k.parquet"
    pd.DataFrame([{"question": "q", "answer": "work #### 1,234"}]).to_parquet(parquet)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda *a, **kw: str(parquet))
    assert datasets.gsm8k_rows() == {
        s: [{"payload": {"prompt": "q", "answer": "1234"}}] for s in ("train", "test")
    }
    upload = tmp_path / "data.jsonl"
    upload.write_text('{"prompt":"q"}\n')
    assert datasets.read_rows(upload, upload.name) == [{"payload": {"prompt": "q"}}]


@pytest.mark.parametrize("consumer", ["catalog", "rollout"])
@pytest.mark.parametrize("valid", [True, False])
def test_import_consumers_check_metadata_without_source_resolution(
    consumer, valid, toolkit_installation, monkeypatch
):
    calls = []
    direct = toolkit_installation
    if not valid:
        direct["vcs_info"]["commit_id"] = "0" * 40

    def forbidden(*args, **kwargs):
        raise AssertionError("import consumers must not resolve source")

    monkeypatch.setattr(toolkit_source, "resolve_source", forbidden)
    # Stub the imported object, not validate_installation. The bad metadata path must
    # stop before either object is used, even when its module is already loaded.
    if consumer == "catalog":
        from agentcore_rl_toolkit.rollout_gateway import response_schemas

        class Registry(dict):
            def get(self, key):
                calls.append(key)
                return super().get(key)

        digest = hashlib.sha256(b"fixture").hexdigest()
        monkeypatch.setattr(response_schemas, "_TEMPLATE_HASHES", Registry({digest: "qwen3"}))

        def operation():
            return models.schema_for_template("fixture")

        expected = "qwen3"
    else:
        import agentcore_rl_toolkit

        expected = object()
        monkeypatch.setattr(
            agentcore_rl_toolkit, "RolloutClient", lambda **kw: calls.append(kw) or expected
        )

        def operation():
            return agents.rollout_client(region="fixture")

    if valid:
        assert operation() == expected
        assert len(calls) == 1
    else:
        with pytest.raises(AppError) as caught:
            operation()
        assert caught.value.code == "toolkit.source_invalid"
        assert caught.value.detail == {"reason": "installed_direct_url"}
        assert not calls
    assert not (get_settings().data_dir / "cache").exists()


def _stub_preflight(stub_aws, monkeypatch):
    from app.catalog import instances
    from app.services import tools

    stub_aws({"sts": StubClient(get_caller_identity={"Account": "123456789012", "Arn": "arn:x"})})
    monkeypatch.setattr(instances, "cluster_quotas", lambda region: {})
    monkeypatch.setattr(tools, "tool_status", lambda: {})
    monkeypatch.setattr(project.shutil, "which", lambda name: None)


@pytest.mark.parametrize(
    "state,status",
    [
        ("cold", "warn"),
        ("warm", "ok"),
        ("bad-install", "fail"),
        ("bad-override", "fail"),
        ("bad-cache", "fail"),
    ],
)
def test_meta_health_and_toolkit_preflight_are_read_only(
    state, status, toolkit_repo, toolkit_installation, client, stub_aws, monkeypatch
):
    f = toolkit_repo
    _stub_preflight(stub_aws, monkeypatch)
    expected_path = f.root
    if state == "cold":
        f.root.rename(f.root.parent / "fixture-away")
    elif state == "bad-install":
        toolkit_installation.clear()
        toolkit_installation.update({"url": "file:///fixture", "dir_info": {"editable": True}})
    elif state == "bad-override":
        expected_path = f.root.parent / "missing-override"
        get_settings().toolkit_path = expected_path
    elif state == "bad-cache":
        (f.root / "README.md").write_text("dirty")
    before = snapshot(f.root.parent)

    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostics must not acquire or copy source")

    for name in ("resolve_source", "_fetch", "copy_tracked"):
        monkeypatch.setattr(toolkit_source, name, forbidden)
    assert client.get("/api/health").json() == {"ok": True}
    response = client.get("/api/meta")
    assert response.status_code == 200
    meta = response.json()
    assert set(meta) == {
        "version",
        "default_region",
        "resource_tag",
        "toolkit_path",
        "toolkit_present",
        "run_mode",
    }
    assert meta["toolkit_path"] == str(expected_path)
    assert meta["toolkit_present"] is (state == "warm")
    response = client.post("/api/setup/preflight", json={"region": "us-east-1"})
    assert response.status_code == 200
    result = response.json()
    check = next(c for c in result["checks"] if c["id"] == "toolkit")
    assert set(check) == {"id", "status", "message", "detail"}
    assert check["status"] == status
    assert result["ok"] is (status != "fail")
    if status == "warn":
        assert "fetched on first use" in check["message"]
    if status == "fail":
        assert check["detail"]["reason"] in {
            "installed_direct_url",
            "checkout_root",
            "dirty_checkout",
        }
    assert snapshot(f.root.parent) == before


def test_malformed_declaration_maps_to_preflight_failure(
    toolkit_repo, client, stub_aws, monkeypatch
):
    _stub_preflight(stub_aws, monkeypatch)
    toolkit_source._PYPROJECT.write_text("[tool.uv.sources]\n")
    check = next(
        c
        for c in client.post("/api/setup/preflight", json={}).json()["checks"]
        if c["id"] == "toolkit"
    )
    assert check["status"] == "fail"
    assert check["detail"] == {"reason": "source_declaration"}
    response = client.get("/api/meta")
    assert response.status_code == 400 and response.json()["code"] == "toolkit.source_invalid"


def test_default_harness_blocks_toolkit_fetch_and_uvbuild(tmp_path):
    with pytest.raises(AssertionError, match="Git fetch/clone"):
        toolkit_source._fetch(
            tmp_path, toolkit_source.source_spec()[0], "0" * 40, time.monotonic() + 10
        )
    with pytest.raises(AssertionError, match="uv build"):
        subprocess.run(["uv", "build", "--wheel"], cwd=tmp_path, check=True)
    with pytest.raises(AssertionError, match="Git fetch/clone"):
        datasets.officebench_checkout()
