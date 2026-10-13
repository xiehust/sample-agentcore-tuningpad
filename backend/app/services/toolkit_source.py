"""Pinned toolkit source, shared by source consumers and read-only diagnostics.

Only pyproject owns the URL/revision. No import-time I/O, sibling fallback, build,
installation, or cloud calls. Private copies expand only validated tracked links.
This is a local POSIX cache, not an adversarial filesystem/supply-chain attestation.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import tomllib
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

from ..core.config import REPO_ROOT, get_settings
from ..core.errors import AppError

_PYPROJECT = REPO_ROOT / "backend" / "pyproject.toml"
_GIT_TIMEOUT = 120.0
_LOCK_TIMEOUT = 60.0
_RESOLVE_TIMEOUT = 300.0
_INSPECT_TIMEOUT = 10.0
_thread_lock = threading.Lock()
_REQUIRED_FILES = (
    "pyproject.toml",
    "README.md",
    "uv.lock",
    "src/agentcore_rl_toolkit/__init__.py",
    "examples/strands_math_agent/reward.py",
    "examples/strands_math_agent/models.py",
    "examples/strands_officebench_agent/rl_app.py",
    "examples/strands_officebench_agent/reward.py",
    "examples/strands_officebench_agent/models.py",
    "examples/strands_officebench_agent/utils.py",
    "examples/strands_officebench_agent/tools.py",
    "examples/strands_officebench_agent/preprocess.py",
    "patches/apply-megatron-bridge-cp-clamp.sh",
    "patches/megatron-bridge-cp-chunk1-clamp.diff",
)


def _error(kind: str, reason: str) -> AppError:
    # Never attach a subprocess argv, stderr, arbitrary URL or filesystem exception.
    return AppError(
        f"toolkit.source_{kind}",
        {
            "invalid": "Toolkit source or installed package does not match the pinned Git source",
            "unavailable": "Toolkit source is unavailable",
            "timeout": "Timed out while checking or obtaining toolkit source",
        }[kind],
        detail={"reason": reason},
    )


def _github_repo(url: str) -> str:
    """Accept credential-free GitHub HTTPS and Git SSH identities, not URL rewrites."""
    if not isinstance(url, str):
        raise _error("invalid", "repository_url")
    if url.startswith("git@github.com:"):
        url = "ssh://git@github.com/" + url.removeprefix("git@github.com:")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.hostname == "github.com"
            and not parsed.query
            and not parsed.fragment
            and parsed.password is None
            and (
                (
                    parsed.scheme == "https"
                    and parsed.username is None
                    and parsed.port in (None, 443)
                )
                or (
                    parsed.scheme == "ssh"
                    and parsed.username == "git"
                    and parsed.port in (None, 22)
                )
            )
        )
        path = parsed.path.removesuffix("/").removesuffix(".git")
        if valid and re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
            return path.lower()
    except ValueError:
        pass
    raise _error("invalid", "repository_url")


def source_spec() -> tuple[str, str]:
    """Read (Git URL, full revision) from pyproject; never create directories or fetch."""
    try:
        source = tomllib.loads(_PYPROJECT.read_text())["tool"]["uv"]["sources"][
            "agentcore-rl-toolkit"
        ]
        url, revision = source["git"], source["rev"]
        if set(source) != {"git", "rev"} or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError
        _github_repo(url)
        # Acquisition always uses the declared HTTPS URL, never an SSH agent or local path.
        if not url.startswith("https://"):
            raise ValueError
        return url, revision
    except (OSError, ValueError, KeyError, TypeError):
        raise _error("invalid", "source_declaration") from None


def _check_installation(url: str, revision: str) -> None:
    try:
        direct = json.loads(
            metadata.distribution("agentcore-rl-toolkit").read_text("direct_url.json")
        )
        vcs = direct["vcs_info"]
        if (
            _github_repo(direct["url"]) != _github_repo(url)
            or vcs["vcs"] != "git"
            or vcs["commit_id"] != revision
            or "dir_info" in direct
        ):
            raise ValueError
    except (metadata.PackageNotFoundError, OSError, ValueError, KeyError, TypeError, AppError):
        raise _error("invalid", "installed_direct_url") from None


def validate_installation() -> None:
    """Check only Git direct_url URL/vcs/commit, without importing toolkit modules."""
    _check_installation(*source_spec())


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _error("timeout", "deadline")
    return remaining


@contextmanager
def _io_errors():
    try:
        yield
    except OSError:
        raise _error("unavailable", "local_io") from None


def _git_env() -> dict[str, str]:
    # Clear repository/index/object/config/URL injection as well as Git tracing and TLS overrides.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="/bin/false",
        SSH_ASKPASS="/bin/false",
        GIT_SSH_COMMAND="ssh -T -oBatchMode=yes -oStrictHostKeyChecking=yes",
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_REPLACE_OBJECTS="1",
        LC_ALL="C",
    )
    return env


def _git(root: Path, *args: str, deadline: float, failure: str = "invalid") -> bytes:
    timeout = min(_GIT_TIMEOUT, _remaining(deadline))
    argv = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "credential.helper=",
        "-c",
        "http.sslVerify=true",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.fileMode=true",
        "-c",
        "core.symlinks=true",
        *args,
    ]
    try:
        with subprocess.Popen(
            argv,
            cwd=root,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as proc:
            try:
                stdout, _ = proc.communicate(timeout=timeout)
            except BaseException as exc:
                # Git's HTTPS/SSH helper may outlive its parent unless the whole group is stopped.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise _error("timeout", "git_command") from None
                raise
            if proc.returncode:
                raise _error(failure, "git_command")
            return stdout
    except OSError:
        raise _error("unavailable", "git_execution") from None


def _fetch(root: Path, url: str, revision: str, deadline: float) -> None:
    """Single acquisition seam; tests replace the remote with a temporary local Git fixture."""
    _git(
        root,
        "fetch",
        "--no-tags",
        "--depth=1",
        "--no-recurse-submodules",
        "--",
        url,
        revision,
        deadline=deadline,
        failure="unavailable",
    )


def _clean(root: Path, deadline: float) -> None:
    if _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        deadline=deadline,
    ):
        raise _error("invalid", "dirty_checkout")
    # These index bits would hide worktree changes from status/diff.
    flags = _git(root, "ls-files", "-v", "-z", deadline=deadline).split(b"\0")
    if any(entry and not entry.startswith(b"H ") for entry in flags):
        raise _error("invalid", "hidden_index_entries")


def _tracked_files(root: Path, deadline: float) -> list[tuple[Path, Path]]:
    """Return (copy-relative name, source file), safely expanding tracked symlinks.

    No blob reads or content manifest: Git owns commit/dirty validation. The small
    directory graph ensures directory links cannot introduce untracked files or cycles.
    """
    entries = _git(root, "ls-tree", "-rz", "--full-tree", "HEAD", deadline=deadline)
    modes: dict[Path, bytes] = {}
    directories = {Path(".")}
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        header, name = entry.split(b"\t", 1)
        mode = header.split()[0]
        rel = Path(os.fsdecode(name))
        if (
            mode not in {b"100644", b"100755", b"120000"}
            or rel.is_absolute()
            or any(part in {"..", ".git"} for part in rel.parts)
        ):
            raise _error("invalid", "unsupported_tracked_entry")
        modes[rel] = mode
        directories.update(rel.parents)
    if not modes:
        raise _error("invalid", "empty_tree")
    for directory in directories:
        path = root / directory
        if path.is_symlink() or not path.is_dir():
            raise _error("invalid", "tracked_directory")
    for rel, mode in modes.items():
        _remaining(deadline)
        path = root / rel
        try:
            actual_mode = path.lstat().st_mode
            if mode == b"120000":
                if not stat.S_ISLNK(actual_mode) or Path(os.readlink(path)).is_absolute():
                    raise _error("invalid", "unsafe_link")
            elif not stat.S_ISREG(actual_mode):
                raise _error("invalid", "tracked_file_type")
        except (FileNotFoundError, ValueError, RuntimeError):
            raise _error("invalid", "unsafe_or_missing_input") from None

    def link_target(rel: Path, seen: frozenset[Path]) -> Path:
        # Validate every hop, not just Path.resolve()'s final target: an ignored
        # intermediary symlink must not steer a tracked link to some other input.
        current = Path(".")
        for part in rel.parts:
            _remaining(deadline)
            if current not in directories:
                raise _error("invalid", "link_parent_not_directory")
            if part == "..":
                if current == Path("."):
                    raise _error("invalid", "escaping_link")
                current = current.parent
                continue
            current /= part
            if modes.get(current) == b"120000":
                if current in seen:
                    raise _error("invalid", "cyclic_link")
                target = Path(os.readlink(root / current))
                if target.is_absolute():
                    raise _error("invalid", "unsafe_link")
                current = link_target(current.parent / target, seen | {current})
            elif current not in modes and current not in directories:
                raise _error("invalid", "untracked_link_target")
        return current

    targets = {
        rel: link_target(rel, frozenset()) for rel, mode in modes.items() if mode == b"120000"
    }
    children: dict[Path, list[Path]] = {d: [] for d in directories}
    for rel in (directories | modes.keys()) - {Path(".")}:
        children[rel.parent].append(rel)
    files: list[tuple[Path, Path]] = []

    def walk(rel: Path, dest: Path, ancestors: frozenset[Path]) -> None:
        _remaining(deadline)
        target = targets.get(rel, rel)
        if target in directories:
            if target in ancestors:
                raise _error("invalid", "cyclic_directory_link")
            for child in sorted(children[target]):
                walk(child, dest / child.name, ancestors | {target})
        else:
            files.append((dest, root / target))

    walk(Path("."), Path("."), frozenset())
    names = {str(rel) for rel, _ in files}
    if not set(_REQUIRED_FILES) <= names:
        raise _error("invalid", "missing_required_inputs")
    return files


def _validate(root: Path, url: str, revision: str, deadline: float) -> list[tuple[Path, Path]]:
    if root.is_symlink() or not root.is_dir() or not (root / ".git").exists():
        raise _error("invalid", "checkout_root")
    root = root.resolve()
    top = os.fsdecode(_git(root, "rev-parse", "--show-toplevel", deadline=deadline)).strip()
    if Path(top).resolve() != root:
        raise _error("invalid", "checkout_root")
    origin = os.fsdecode(
        _git(
            root,
            "config",
            "--local",
            "--no-includes",
            "--get-all",
            "remote.origin.url",
            deadline=deadline,
        )
    ).strip()
    if _github_repo(origin) != _github_repo(url):
        raise _error("invalid", "origin_mismatch")
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}", deadline=deadline).strip()
    if head != revision.encode():
        raise _error("invalid", "revision_mismatch")
    _clean(root, deadline)
    files = _tracked_files(root, deadline)
    _clean(root, deadline)
    return files


def _location(revision: str) -> Path:
    settings = get_settings()
    return (settings.toolkit_path or settings.data_dir / "cache" / "toolkit" / revision).absolute()


def inspect_source() -> tuple[Path, bool, AppError | None]:
    """Read-only (configured/derived path, present-and-valid, failure).

    An uncached default source returns (path, False, None), not an offline guess.
    Callers map this internal tuple onto their existing Meta/Check contracts.
    A malformed declaration raises AppError (there is then no authoritative cache path).
    """
    deadline = time.monotonic() + _INSPECT_TIMEOUT
    url, revision = source_spec()
    root = _location(revision)
    try:
        with _io_errors():
            _check_installation(url, revision)
            if get_settings().toolkit_path is None and not os.path.lexists(root):
                return root, False, None
            _validate(root, url, revision, deadline)
        return root, True, None
    except AppError as exc:
        return root, False, exc


@contextmanager
def _cache_lock(parent: Path, revision: str, deadline: float):
    deadline = min(deadline, time.monotonic() + _LOCK_TIMEOUT)
    if not _thread_lock.acquire(timeout=_remaining(deadline)):
        raise _error("timeout", "thread_lock")
    try:
        parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(parent / f"{revision}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise _error("invalid", "cache_lock_file")
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(0.05, _remaining(deadline)))
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        # Never unlink a lock inode: another process may already have it open.
    finally:
        _thread_lock.release()


def resolve_source(expected_revision: str | None = None) -> tuple[Path, str]:
    """Return a verified (root, revision); only a missing default cache may fetch.

    Explicit local supply always wins, including its validation errors. Consumers
    must use copy_tracked before build/import/packaging, never write into this root.
    """
    deadline = time.monotonic() + _RESOLVE_TIMEOUT
    url, revision = source_spec()
    if expected_revision is not None and expected_revision != revision:
        raise _error("invalid", "expected_revision_mismatch")
    _check_installation(url, revision)
    root = _location(revision)
    with _io_errors():
        if get_settings().toolkit_path is not None:
            _validate(root, url, revision, deadline)
            return root.resolve(), revision
        with _cache_lock(root.parent, revision, deadline):
            if os.path.lexists(root):
                _validate(root, url, revision, deadline)
            else:
                with tempfile.TemporaryDirectory(prefix=f".{revision}-", dir=root.parent) as tmp:
                    staging = Path(tmp)
                    _git(staging, "init", "--template=", deadline=deadline, failure="unavailable")
                    _git(staging, "remote", "add", "origin", url, deadline=deadline)
                    _fetch(staging, url, revision, deadline)
                    _git(staging, "checkout", "--detach", revision, deadline=deadline)
                    _validate(staging, url, revision, deadline)
                    if os.path.lexists(root):
                        raise _error("invalid", "cache_destination_exists")
                    staging.rename(root)
            return root.resolve(), revision


def copy_tracked(root: Path, dest: Path, *, expected_revision: str) -> Path:
    """Copy verified tracked inputs into an absent task-private directory.

    Revalidate before and after copying; expand safe internal links for zip/build
    consumers, exclude .git and ignored residue. On failure remove only our new copy.
    Existing destinations and copies inside the source are rejected, never removed.
    """
    deadline = time.monotonic() + _RESOLVE_TIMEOUT
    url, revision = source_spec()
    if expected_revision != revision:
        raise _error("invalid", "expected_revision_mismatch")
    _check_installation(url, revision)
    with _io_errors():
        root = root.absolute()
        files = _validate(root, url, revision, deadline)
        if dest.resolve().is_relative_to(root.resolve()):
            raise _error("invalid", "copy_destination_inside_source")
        if os.path.lexists(dest):
            raise _error("invalid", "copy_destination_exists")
        dest.mkdir(parents=True)
        try:
            for rel, source in files:
                _remaining(deadline)
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            _validate(root, url, revision, deadline)
        except BaseException:
            shutil.rmtree(dest)
            raise
    return dest
