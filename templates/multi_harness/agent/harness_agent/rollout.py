"""One rollout: stage the Task, run the Harness, then run the Verifier and score.

The Harness never sees the Verifier: `task.toml` (which holds the expected answer in the
Verifier's env) is read into memory only, and `tests/` is downloaded after the Harness
exits. Each AgentCore session is its own microVM; directories are still cleared first.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import claude_code, reward

HARNESSES = {claude_code.NAME: claude_code}
DEFAULT_HARNESS_TIMEOUT_S = 1200.0
DEFAULT_VERIFIER_TIMEOUT_S = 120.0


class HarnessUnavailable(RuntimeError):
    """The payload names a Harness this Agent image does not contain."""


@dataclass(frozen=True)
class Paths:
    workdir: Path  # the Harness's cwd; the Task asks for /workdir/answer.txt
    inputs: Path  # the Task's input files (/home/user/input)
    tests: Path  # the Verifier, staged after the Harness exits (/tests)
    logs: Path  # the Verifier writes logs/verifier/reward.txt (/logs)


CONTAINER_PATHS = Paths(
    workdir=Path("/workdir"),
    inputs=Path("/home/user/input"),
    tests=Path("/tests"),
    logs=Path("/logs"),
)


@dataclass(frozen=True)
class Completed:
    returncode: int | None  # None: killed at the timeout
    stdout: str
    stderr: str


Run = Callable[..., Completed]


def run_process(cmd: list[str], *, env: dict, cwd: Path, stdin: str, timeout: float) -> Completed:
    """Run in its own process group so a timeout also kills whatever it spawned."""
    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        return Completed(proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return Completed(None, out, err)


def _clear(paths: Paths) -> None:
    for d in (paths.workdir, paths.inputs, paths.tests, paths.logs):
        d.mkdir(parents=True, exist_ok=True)
        for child in d.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()


def _pick_harness(payload: dict[str, Any], installed: list[str]):
    name = payload.get("harness") or (installed[0] if len(installed) == 1 else None)
    if name not in installed or name not in HARNESSES:
        raise HarnessUnavailable(f"harness {name!r} is not installed (have {installed})")
    return HARNESSES[name]


def _verifier_reward(paths: Paths) -> float | None:
    try:
        return float((paths.logs / "verifier" / "reward.txt").read_text().strip())
    except (OSError, ValueError):
        return None


def run_rollout(
    payload: dict[str, Any],
    *,
    params: dict[str, Any],
    paths: Paths,
    read: Callable[[str], bytes],
    fetch_tree: Callable[[str, Path], None],
    run: Run,
    region: str,
) -> dict[str, Any]:
    config = payload["_config"]
    harness = _pick_harness(payload, params.get("harnesses") or [claude_code.NAME])
    task_uri = payload["task_uri"]
    instruction = read(f"{task_uri}instruction.md").decode()
    task = tomllib.loads(read(f"{task_uri}task.toml").decode())

    _clear(paths)
    fetch_tree(payload["input_uri"], paths.inputs)

    launch = harness.build(
        instruction, config, region=region, max_turns=int(params["harness_max_turns"])
    )
    agent_timeout = (task.get("agent") or {}).get("timeout_sec") or DEFAULT_HARNESS_TIMEOUT_S
    done = run(
        launch.cmd,
        env=launch.env,
        cwd=paths.workdir,
        stdin=launch.stdin,
        timeout=min(float(agent_timeout), DEFAULT_HARNESS_TIMEOUT_S),
    )
    parsed = harness.parse(done.stdout)

    # The Harness has exited: only now does the Verifier exist on disk.
    fetch_tree(f"{task_uri}tests/", paths.tests)
    verifier = task.get("verifier") or {}
    venv = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/home/user"),
        "LANG": "C.UTF-8",
        # test.sh may `pip install` what the image already has; never reach for PyPI
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        **{k: str(v) for k, v in (verifier.get("env") or {}).items()},
    }
    run(
        ["bash", str(paths.tests / "test.sh")],
        env=venv,
        cwd=paths.workdir,
        stdin="",
        timeout=float(verifier.get("timeout_sec") or DEFAULT_VERIFIER_TIMEOUT_S),
    )
    verifier_reward = _verifier_reward(paths)
    score = reward.score(
        verifier_reward,
        parsed.tool_calls,
        cap=float(params["tool_bonus_cap"]),
        budget=int(params["tool_call_budget"]),
    )
    out: dict[str, Any] = {
        "reward": score,
        "harness": harness.NAME,
        "metrics": {
            "tool_calls": parsed.tool_calls,
            "verifier_reward": verifier_reward,
            "correct": int(verifier_reward is not None and verifier_reward >= 1.0),
        },
        "stop_reason": "harness_timeout" if done.returncode is None else "completed",
    }
    if verifier_reward is None:
        out["stop_reason"] = "verifier_no_result"
    if parsed.warning:
        out["warning"] = parsed.warning
    return out
