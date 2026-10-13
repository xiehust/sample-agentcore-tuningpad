"""Toolkit source tests use only disposable local Git repos and installation metadata stubs."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.services import toolkit_source as source


def git(root, *args):
    return (
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
            cwd=root,
            env=source._git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=10,
        )
        .stdout.decode()
        .strip()
    )


def assert_error(kind, operation):
    with pytest.raises(AppError) as caught:
        operation()
    assert caught.value.code == f"toolkit.source_{kind}"
    assert "secret" not in str(caught.value.detail)
    return caught.value


@pytest.fixture
def fixture_source(tmp_path, monkeypatch):
    url, _ = source.source_spec()
    repo = tmp_path / "remote"
    repo.mkdir()
    git(repo, "init", "--template=")
    git(repo, "remote", "add", "origin", url)
    files = [
        "pyproject.toml",
        "README.md",
        "uv.lock",
        "src/agentcore_rl_toolkit/__init__.py",
        "patches/apply-megatron-bridge-cp-clamp.sh",
        "patches/megatron-bridge-cp-chunk1-clamp.diff",
        "examples/strands_math_agent/models.py",
        "examples/strands_math_agent/reward.py",
        *[
            f"examples/strands_officebench_agent/{name}.py"
            for name in ("rl_app", "reward", "models", "utils", "tools", "preprocess")
        ],
    ]
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# fixture {name}\n")
    (repo / ".gitignore").write_text("dist/\n__pycache__/\nignored*\n")
    (repo / "AGENTS.md").write_text("tracked instructions\n")
    (repo / "CLAUDE.md").symlink_to("AGENTS.md")
    (repo / "nested").mkdir()
    (repo / "nested" / "link").symlink_to("../AGENTS.md")
    (repo / "bin").mkdir()
    (repo / "bin" / "tool").write_text("fixture executable\n")
    (repo / "bin" / "tool").chmod(0o755)
    declaration = tmp_path / "source.toml"
    settings = SimpleNamespace(data_dir=tmp_path / "data", toolkit_path=None)
    fixture = SimpleNamespace(
        repo=repo, url=url, calls=[], settings=settings, declaration=declaration
    )

    def commit():
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "fixture")
        fixture.revision = git(repo, "rev-parse", "HEAD")
        declaration.write_text(
            "[tool.uv.sources]\nagentcore-rl-toolkit = "
            f'{{ git = "{url}", rev = "{fixture.revision}" }}\n'
        )
        fixture.direct = {"url": url, "vcs_info": {"vcs": "git", "commit_id": fixture.revision}}
        fixture.cache = settings.data_dir / "cache" / "toolkit" / fixture.revision

    fixture.commit = commit
    commit()
    monkeypatch.setattr(source, "_PYPROJECT", declaration)
    monkeypatch.setattr(source, "get_settings", lambda: settings)
    monkeypatch.setattr(
        source.metadata,
        "distribution",
        lambda name: SimpleNamespace(read_text=lambda file: json.dumps(fixture.direct)),
    )

    def fetch(root, fetched_url, revision, deadline):
        assert fetched_url == fixture.url and revision == fixture.revision
        assert not fixture.cache.exists()  # no partial public root
        fixture.calls.append((fetched_url, revision))
        source._git(
            root,
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--no-tags",
            "--depth=1",
            "--no-recurse-submodules",
            "--",
            str(repo),
            revision,
            deadline=deadline,
            failure="unavailable",
        )

    fixture.production_fetch = source._fetch
    monkeypatch.setattr(source, "_fetch", fetch)
    fixture.fetch = fetch
    return fixture


def test_fetch_contract_is_one_explicit_sha_without_fallback(fixture_source, monkeypatch):
    f = fixture_source
    calls = []

    def record(root, *args, **kwargs):
        calls.append((root, args, kwargs))
        raise source._error("unavailable", "fixture_offline")

    monkeypatch.setattr(source, "_git", record)
    assert_error("unavailable", lambda: f.production_fetch(f.repo, f.url, f.revision, 123.0))
    assert calls == [
        (
            f.repo,
            ("fetch", "--no-tags", "--depth=1", "--no-recurse-submodules", "--", f.url, f.revision),
            {"deadline": 123.0, "failure": "unavailable"},
        )
    ]


def test_relative_override_is_relative_to_cwd(fixture_source, monkeypatch):
    f = fixture_source
    monkeypatch.chdir(f.repo / "nested")
    f.settings.toolkit_path = f.repo.relative_to(f.repo) / ".."
    assert source.resolve_source() == (f.repo, f.revision)
    assert not f.calls


def test_missing_distribution_or_direct_url_is_invalid(fixture_source, monkeypatch):
    def missing(name):
        raise source.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(source.metadata, "distribution", missing)
    assert_error("invalid", source.validate_installation)
    monkeypatch.setattr(
        source.metadata, "distribution", lambda name: SimpleNamespace(read_text=lambda file: None)
    )
    assert_error("invalid", source.validate_installation)


def snapshot(root):
    return {
        str(p.relative_to(root)): (
            os.readlink(p) if p.is_symlink() else p.read_bytes(),
            p.lstat().st_mtime_ns,
        )
        for p in root.rglob("*")
        if p.is_file() or p.is_symlink()
    }


def test_default_config_has_no_sibling(monkeypatch):
    monkeypatch.delenv("TUNINGPAD_TOOLKIT_PATH", raising=False)
    assert Settings().toolkit_path is None


def test_spec_and_inspect_are_read_only(fixture_source):
    f = fixture_source
    assert source.source_spec() == (f.url, f.revision)
    assert source.inspect_source() == (f.cache, False, None)
    assert not f.settings.data_dir.exists()
    assert not f.calls
    f.settings.toolkit_path = f.repo
    before = snapshot(f.repo)
    assert source.inspect_source() == (f.repo, True, None)
    assert snapshot(f.repo) == before
    assert not f.settings.data_dir.exists()


@pytest.mark.parametrize(
    "value",
    [
        '{ path = "secret", editable = true }',
        '{ git = "https://github.com/x/y.git", rev = "main" }',
        '{ git = "https://secret@github.com/x/y.git", rev = "' + "a" * 40 + '" }',
        '{ git = "https://github.com/x/y.git", rev = "' + "a" * 40 + '", branch = "main" }',
    ],
)
def test_invalid_declaration(fixture_source, value):
    fixture_source.declaration.write_text("[tool.uv.sources]\nagentcore-rl-toolkit = " + value)
    assert_error("invalid", source.source_spec)
    assert not fixture_source.settings.data_dir.exists()


@pytest.mark.parametrize(
    "direct",
    [
        None,
        {},
        [],
        {"url": "file:///secret", "dir_info": {"editable": True}},
        {"url": None, "vcs_info": {}},
        {"url": "https://secret:secret@github.com/x/y", "vcs_info": {}},
    ],
)
def test_missing_or_non_git_metadata(fixture_source, direct):
    f = fixture_source
    f.direct = direct
    assert_error("invalid", source.validate_installation)
    assert_error("invalid", source.resolve_source)
    path, present, error = source.inspect_source()
    assert path == f.cache and not present and error.code == "toolkit.source_invalid"
    assert not f.settings.data_dir.exists()


@pytest.mark.parametrize("change", ["url", "vcs", "commit_id"])
def test_metadata_mismatch(fixture_source, change):
    f = fixture_source
    if change == "url":
        f.direct["url"] = "https://github.com/another/repository.git"
    else:
        f.direct["vcs_info"][change] = "incorrect"
    assert_error("invalid", source.resolve_source)
    assert not f.calls


def test_metadata_does_not_require_requested_revision(fixture_source):
    source.validate_installation()
    fixture_source.direct["vcs_info"]["requested_revision"] = "not-checked"
    source.validate_installation()


def test_cold_then_offline_hot_cache(fixture_source, monkeypatch):
    f = fixture_source
    assert source.resolve_source() == (f.cache, f.revision)
    assert f.calls == [(f.url, f.revision)]
    assert git(f.cache, "rev-parse", "HEAD") == f.revision
    assert (f.cache / ".git" / "HEAD").read_text().strip() == f.revision
    monkeypatch.setattr(source, "_fetch", lambda *args: pytest.fail("hot cache fetched"))
    before = snapshot(f.cache)
    assert source.resolve_source(f.revision) == (f.cache, f.revision)
    assert source.inspect_source() == (f.cache, True, None)
    assert snapshot(f.cache) == before
    assert_error("invalid", lambda: source.resolve_source(f.revision[:10]))


def test_cold_fetch_uses_pinned_commit_not_remote_head(fixture_source):
    f = fixture_source
    pinned = f.revision
    (f.repo / "newer-file").write_text("not part of the pinned commit")
    git(f.repo, "add", "newer-file")
    git(f.repo, "commit", "-m", "newer remote head")
    assert git(f.repo, "rev-parse", "HEAD") != pinned
    root, revision = source.resolve_source()
    assert revision == pinned and git(root, "rev-parse", "HEAD") == pinned
    assert not (root / "newer-file").exists()
    assert f.calls == [(f.url, pinned)]


def test_offline_cold_failure_has_no_fallback(fixture_source, monkeypatch):
    f = fixture_source
    calls = []

    def unavailable(root, *args):
        calls.append(root)
        source._git(
            root,
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--",
            "/no/secret/repo",
            f.revision,
            deadline=time.monotonic() + 10,
            failure="unavailable",
        )

    monkeypatch.setattr(source, "_fetch", unavailable)
    assert_error("unavailable", source.resolve_source)
    assert len(calls) == 1 and not f.cache.exists()
    assert list(f.cache.parent.iterdir()) == [f.cache.with_suffix(".lock")]


@pytest.mark.parametrize("damage", ["unknown", "empty", "dirty", "missing_git", "symlink"])
def test_unknown_or_damaged_cache_is_preserved(fixture_source, damage):
    f = fixture_source
    if damage in {"dirty", "missing_git"}:
        source.resolve_source()
        if damage == "dirty":
            (f.cache / "README.md").write_text("changed")
        else:
            (f.cache / ".git").rename(f.cache / "saved-git")
    else:
        f.cache.parent.mkdir(parents=True)
        if damage == "symlink":
            f.cache.symlink_to(f.repo, target_is_directory=True)
        else:
            f.cache.mkdir()
            if damage == "unknown":
                (f.cache / "keep").write_text("unknown content")
    before = snapshot(f.cache)
    calls = len(f.calls)
    assert_error("invalid", source.resolve_source)
    assert snapshot(f.cache) == before and len(f.calls) == calls


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/{repo}.git",
        "https://github.com/{repo}",
        "git@github.com:{repo}.git",
        "ssh://git@github.com/{repo}.git",
        "ssh://git@github.com:22/{repo}.git",
    ],
)
def test_equivalent_origins_and_local_override(fixture_source, origin):
    f = fixture_source
    git(f.repo, "remote", "set-url", "origin", origin.format(repo=source._github_repo(f.url)[1:]))
    f.settings.toolkit_path = f.repo
    before = snapshot(f.repo)
    assert source.resolve_source() == (f.repo, f.revision)
    assert not f.calls and not f.settings.data_dir.exists()
    assert snapshot(f.repo) == before


@pytest.mark.parametrize(
    "damage",
    [
        "wrong_origin",
        "wrong_head",
        "wrong_root",
        "missing",
        "dirty",
        "staged",
        "untracked",
        "assume_unchanged",
        "skip_worktree",
        "missing_required",
    ],
)
def test_invalid_override_is_not_hidden_by_hot_cache(fixture_source, damage):
    f = fixture_source
    source.resolve_source()
    f.settings.toolkit_path = f.repo
    if damage == "wrong_origin":
        git(
            f.repo, "remote", "set-url", "origin", "https://secret:secret@github.com/other/repo.git"
        )
    elif damage == "wrong_head":
        (f.repo / "extra").write_text("second commit")
        git(f.repo, "add", "extra")
        git(f.repo, "commit", "-m", "second")
    elif damage == "wrong_root":
        f.settings.toolkit_path = f.repo / "src"
    elif damage == "missing":
        f.settings.toolkit_path = f.repo / "absent"
    elif damage == "untracked":
        (f.repo / "extra").write_text("untracked")
    elif damage == "missing_required":
        (f.repo / "uv.lock").unlink()
    else:
        if damage in {"assume_unchanged", "skip_worktree"}:
            git(f.repo, "update-index", "--" + damage.replace("_", "-"), "README.md")
        (f.repo / "README.md").write_text("changed")
        if damage == "staged":
            git(f.repo, "add", "README.md")
    before = snapshot(f.repo)
    assert_error("invalid", source.resolve_source)
    assert source.inspect_source()[2].code == "toolkit.source_invalid"
    assert snapshot(f.repo) == before and len(f.calls) == 1


def test_committed_missing_input_rejected(fixture_source):
    f = fixture_source
    (f.repo / "uv.lock").unlink()
    f.commit()
    f.settings.toolkit_path = f.repo
    assert assert_error("invalid", source.resolve_source).detail == {
        "reason": "missing_required_inputs"
    }


def test_tracked_copy_expands_internal_links_and_excludes_residue(fixture_source, tmp_path):
    f = fixture_source
    (f.repo / "dir-link").symlink_to("bin", target_is_directory=True)
    (f.repo / "chained-link").symlink_to("dir-link/tool")
    f.commit()
    f.settings.toolkit_path = f.repo
    (f.repo / "dist").mkdir()
    (f.repo / "dist" / "generated").write_text("ignored build output")
    (f.repo / "ignored-private").write_text("not copied")
    before = snapshot(f.repo)
    root, revision = source.resolve_source()
    dest = tmp_path / "private"
    assert source.copy_tracked(root, dest, expected_revision=revision) == dest
    assert (dest / "CLAUDE.md").read_bytes() == (dest / "AGENTS.md").read_bytes()
    assert (dest / "nested" / "link").read_bytes() == (dest / "AGENTS.md").read_bytes()
    assert (dest / "dir-link" / "tool").read_bytes() == (dest / "bin" / "tool").read_bytes()
    assert (dest / "chained-link").read_bytes() == (dest / "bin" / "tool").read_bytes()
    assert (dest / "bin" / "tool").stat().st_mode & 0o111
    assert not any(p.is_symlink() for p in dest.rglob("*"))
    assert not (dest / ".git").exists() and not (dest / "dist").exists()
    assert not (dest / "ignored-private").exists()
    (dest / "README.md").write_text("consumer build change")
    assert snapshot(f.repo) == before


@pytest.mark.parametrize(
    "target", ["/etc/passwd", "../../outside", "missing", "bad-link", "ignored-target", "."]
)
def test_unsafe_tracked_links_rejected(fixture_source, target):
    f = fixture_source
    (f.repo / "bad-link").symlink_to(target)
    (f.repo / "ignored-target").write_text("ignored")
    f.commit()
    f.settings.toolkit_path = f.repo
    assert_error("invalid", source.resolve_source)
    assert not f.calls


@pytest.mark.parametrize("intermediary", ["AGENTS.md", "absolute", "bin"])
def test_ignored_link_hop_rejected_even_when_final_target_is_tracked(fixture_source, intermediary):
    f = fixture_source
    target = f.repo / "AGENTS.md" if intermediary == "absolute" else intermediary
    (f.repo / "ignored-hop").symlink_to(target)
    (f.repo / "tracked-link").symlink_to(
        "ignored-hop/tool" if intermediary == "bin" else "ignored-hop"
    )
    f.commit()
    f.settings.toolkit_path = f.repo
    assert_error("invalid", source.resolve_source)


def test_mutual_directory_link_cycle_rejected(fixture_source):
    f = fixture_source
    (f.repo / "left").mkdir()
    (f.repo / "right").mkdir()
    (f.repo / "left" / "link").symlink_to("../right")
    (f.repo / "right" / "link").symlink_to("../left")
    f.commit()
    f.settings.toolkit_path = f.repo
    assert_error("invalid", source.resolve_source)


def test_submodules_rejected_without_fetching_them(fixture_source):
    f = fixture_source
    git(f.repo, "update-index", "--add", "--cacheinfo", f"160000,{f.revision},module")
    git(f.repo, "commit", "-m", "gitlink")
    f.revision = git(f.repo, "rev-parse", "HEAD")
    f.declaration.write_text(
        f'[tool.uv.sources]\nagentcore-rl-toolkit = {{ git = "{f.url}", rev = "{f.revision}" }}\n'
    )
    f.direct["vcs_info"]["commit_id"] = f.revision
    f.settings.toolkit_path = f.repo
    assert_error("invalid", source.resolve_source)
    assert not f.calls


@pytest.mark.parametrize("when", ["before", "during"])
def test_copy_rechecks_source_and_cleans_only_owned_destination(
    fixture_source, tmp_path, monkeypatch, when
):
    root, revision = source.resolve_source()
    dest = tmp_path / "copy"
    copy2 = source.shutil.copy2

    def change_after_copy(*args, **kwargs):
        result = copy2(*args, **kwargs)
        (root / "README.md").write_text("concurrent edit")
        return result

    if when == "before":
        (root / "README.md").write_text("edit after resolve")
    else:
        monkeypatch.setattr(source.shutil, "copy2", change_after_copy)
    assert_error("invalid", lambda: source.copy_tracked(root, dest, expected_revision=revision))
    assert not dest.exists() and (root / "README.md").exists()


def test_copy_refuses_existing_inside_and_wrong_revision_destinations(fixture_source, tmp_path):
    root, revision = source.resolve_source()
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("keep")
    for dest, expected in [
        (existing, revision),
        (root / "copy", revision),
        (tmp_path / "wrong", "a" * 40),
    ]:
        assert_error(
            "invalid",
            lambda dest=dest, expected=expected: source.copy_tracked(
                root, dest, expected_revision=expected
            ),
        )
    assert (existing / "keep").read_text() == "keep"
    assert not (root / "copy").exists() and not (tmp_path / "wrong").exists()


def test_inherited_git_configuration_cannot_redirect_checkout(
    fixture_source, tmp_path, monkeypatch
):
    f = fixture_source
    f.settings.toolkit_path = f.repo
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[core]\n worktree = /no/secret\n hooksPath = /no/secret\n fsmonitor = secret\n"
        '[url "https://secret@example.invalid/"]\n insteadOf = https://github.com/\n'
    )
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_VALUE_0",
        "GIT_SSH_COMMAND",
        "GIT_EXEC_PATH",
    ):
        monkeypatch.setenv(name, "/no/secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    assert source.resolve_source() == (f.repo, f.revision)
    env = source._git_env()
    assert "GIT_SSL_NO_VERIFY" not in env and "GIT_INDEX_FILE" not in env
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("operation", ["git", "inspect", "resolve"])
def test_silent_git_timeout_kills_process(fixture_source, monkeypatch, operation):
    f = fixture_source
    real_popen = subprocess.Popen
    processes = []

    def silent_git(argv, **kwargs):
        assert argv[0] == "git" and "http.sslVerify=true" in argv
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["start_new_session"] and not kwargs.get("shell")
        proc = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(source.subprocess, "Popen", silent_git)
    monkeypatch.setattr(source, "_GIT_TIMEOUT", 0.05)
    started = time.monotonic()
    if operation == "inspect":
        f.settings.toolkit_path = f.repo
        monkeypatch.setattr(source, "_GIT_TIMEOUT", 60)
        monkeypatch.setattr(source, "_INSPECT_TIMEOUT", 0.05)
        assert source.inspect_source()[2].code == "toolkit.source_timeout"
        assert not f.settings.data_dir.exists()
    elif operation == "resolve":
        monkeypatch.setattr(source, "_GIT_TIMEOUT", 60)
        monkeypatch.setattr(source, "_RESOLVE_TIMEOUT", 0.05)
        assert_error("timeout", source.resolve_source)
        assert not f.cache.exists()
    else:
        assert_error(
            "timeout", lambda: source._git(f.repo, "status", deadline=time.monotonic() + 60)
        )
    assert time.monotonic() - started < 3
    assert len(processes) == 1 and processes[0].poll() is not None


def test_git_execution_error_is_safe(fixture_source, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("secret executable")

    monkeypatch.setattr(source.subprocess, "Popen", missing)
    assert_error("unavailable", source.resolve_source)


def test_thread_lock_timeout(fixture_source, monkeypatch):
    monkeypatch.setattr(source, "_LOCK_TIMEOUT", 0.03)
    source._thread_lock.acquire()
    try:
        assert assert_error("timeout", source.resolve_source).detail == {"reason": "thread_lock"}
        assert not fixture_source.settings.data_dir.exists()
    finally:
        source._thread_lock.release()


def test_threads_publish_one_complete_checkout(fixture_source, monkeypatch):
    f = fixture_source
    entered, release = threading.Event(), threading.Event()
    barrier = threading.Barrier(3)

    def fetch(*args):
        entered.set()
        assert release.wait(5)
        f.fetch(*args)

    def resolve():
        barrier.wait(timeout=5)
        return source.resolve_source()

    monkeypatch.setattr(source, "_fetch", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        workers = [pool.submit(resolve) for _ in range(2)]
        try:
            barrier.wait(timeout=5)
            assert entered.wait(5)
            assert not f.cache.exists()
        finally:
            release.set()
        assert [worker.result(timeout=10) for worker in workers] == [(f.cache, f.revision)] * 2
    assert f.calls == [(f.url, f.revision)]


def _resolve_child(barrier, results):
    try:
        barrier.wait(timeout=5)
        root, revision = source.resolve_source()
        results.put((str(root), revision, (root / "uv.lock").is_file()))
    except BaseException as exc:
        results.put((type(exc).__name__, str(exc)))


def _stop_children(children):
    for child in children:
        child.join(timeout=5)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)


def test_processes_publish_once_and_keep_lock_inode(fixture_source, monkeypatch):
    f = fixture_source
    ctx = multiprocessing.get_context("fork")
    barrier, results = ctx.Barrier(3), ctx.Queue()
    entered, release, calls = ctx.Event(), ctx.Event(), ctx.Value("i", 0)

    def fetch(*args):
        with calls.get_lock():
            calls.value += 1
        entered.set()
        assert release.wait(5)
        f.fetch(*args)

    monkeypatch.setattr(source, "_fetch", fetch)
    children = [ctx.Process(target=_resolve_child, args=(barrier, results)) for _ in range(2)]
    for child in children:
        child.start()
    try:
        barrier.wait(timeout=5)
        assert entered.wait(5)
        assert not f.cache.exists()
        inode = f.cache.with_suffix(".lock").stat().st_ino
        release.set()
        assert [results.get(timeout=10) for _ in children] == [(str(f.cache), f.revision, True)] * 2
    finally:
        release.set()
        _stop_children(children)
    assert all(child.exitcode == 0 for child in children)
    assert calls.value == 1 and f.cache.with_suffix(".lock").stat().st_ino == inode
    results.close()


def test_process_lock_timeout(fixture_source, monkeypatch):
    f = fixture_source
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    f.cache.parent.mkdir(parents=True)

    def hold_lock():
        with f.cache.with_suffix(".lock").open("w") as lock:
            source.fcntl.flock(lock, source.fcntl.LOCK_EX)
            ready.set()
            assert release.wait(5)

    child = ctx.Process(target=hold_lock)
    child.start()
    try:
        assert ready.wait(5)
        monkeypatch.setattr(source, "_LOCK_TIMEOUT", 0.05)
        assert_error("timeout", source.resolve_source)
        assert not f.cache.exists() and not f.calls
    finally:
        release.set()
        _stop_children([child])
    assert child.exitcode == 0


def test_interruption_cleans_owned_staging_and_retry_succeeds(fixture_source, monkeypatch):
    f = fixture_source
    f.cache.parent.mkdir(parents=True)
    unknown = f.cache.parent / ".unknown-staging"
    unknown.mkdir()
    (unknown / "keep").write_text("keep")

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(source, "_fetch", interrupt)
    with pytest.raises(KeyboardInterrupt):
        source.resolve_source()
    assert not f.cache.exists()
    lock = f.cache.with_suffix(".lock")
    inode = lock.stat().st_ino
    assert set(f.cache.parent.iterdir()) == {unknown, lock}
    monkeypatch.setattr(source, "_fetch", f.fetch)
    assert source.resolve_source() == (f.cache, f.revision)
    assert lock.stat().st_ino == inode and (unknown / "keep").read_text() == "keep"


def test_killed_process_leaves_only_private_staging_and_retry_is_safe(fixture_source, monkeypatch):
    f = fixture_source
    ctx = multiprocessing.get_context("fork")
    entered = ctx.Event()
    barrier, results = ctx.Barrier(2), ctx.Queue()

    def interrupted_fetch(*args):
        entered.set()
        # A killed multiprocessing.Event waiter cannot safely be notified afterwards.
        threading.Event().wait(10)

    monkeypatch.setattr(source, "_fetch", interrupted_fetch)
    child = ctx.Process(target=_resolve_child, args=(barrier, results))
    child.start()
    try:
        barrier.wait(timeout=5)
        assert entered.wait(5)
        assert not f.cache.exists()
        child.terminate()
        child.join(timeout=5)
        assert not child.is_alive()
        stale = set(f.cache.parent.iterdir())
        assert len(stale) == 2  # persistent lock and an unpublished, abandoned staging directory
        monkeypatch.setattr(source, "_fetch", f.fetch)
        assert source.resolve_source() == (f.cache, f.revision)
        assert stale <= set(f.cache.parent.iterdir())  # do not sweep directories we do not own
    finally:
        _stop_children([child])
        results.close()
