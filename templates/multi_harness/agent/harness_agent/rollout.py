"""One rollout: stage the Task, run the Harness, then run the Verifier and score.

The Harness cannot see or influence the Verifier:
- it runs as its own unprivileged user (HARNESS_USER); this process (root) and the
  Verifier's directories are not its own, so it can read neither the agent's environment
  (AWS credentials) nor write /tests or /logs;
- `task.toml` (the expected answer is in the Verifier's env) is only read into memory;
- after it exits, every process of its user is killed, /tests and /logs are cleared, and
  only then is the Verifier downloaded and run, with a fresh HOME and no user site-packages.
Each AgentCore session is its own microVM; directories are still cleared first.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import claude_code, reward
from .procenv import base_env

HARNESSES = {claude_code.NAME: claude_code}
HARNESS_USER = "harness"
DEFAULT_HARNESS_TIMEOUT_S = 1200.0
# TuningPad's smoke waits 600s per rollout; leave room for staging and the Verifier
SMOKE_HARNESS_TIMEOUT_S = 300.0
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


def run_process(
    cmd: list[str],
    *,
    env: dict,
    cwd: Path,
    stdin: str,
    timeout: float,
    user: str | None = None,
) -> Completed:
    """Run in its own process group so a timeout also kills whatever it spawned; with
    `user`, drop to that user first (this process runs as root in the Agent image)."""
    if user:
        cmd = ["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", *cmd]
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


def kill_user_processes(user: str) -> None:
    """Whatever the Harness left behind (detached or not) must not outlive it."""
    subprocess.run(["pkill", "-KILL", "-u", user], check=False)


def _clear(*dirs: Path) -> None:
    for d in dirs:
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
    kill_user: Callable[[str], None],
    region: str,
) -> dict[str, Any]:
    config = payload["_config"]
    harness = _pick_harness(payload, params.get("harnesses") or [claude_code.NAME])
    task_uri = payload["task_uri"]
    instruction = read(f"{task_uri}instruction.md").decode()
    task = tomllib.loads(read(f"{task_uri}task.toml").decode())

    _clear(paths.workdir, paths.inputs, paths.tests, paths.logs)
    fetch_tree(payload["input_uri"], paths.inputs)

    smoke = bool(config.get("smoke"))
    launch = harness.build(
        instruction,
        config,
        region=region,
        max_turns=int(params["harness_max_turns"]),
        smoke_model=params["smoke_bedrock_model"],
    )
    agent_timeout = (task.get("agent") or {}).get("timeout_sec") or DEFAULT_HARNESS_TIMEOUT_S
    cap = SMOKE_HARNESS_TIMEOUT_S if smoke else DEFAULT_HARNESS_TIMEOUT_S
    done = run(
        launch.cmd,
        env=launch.env,
        cwd=paths.workdir,
        stdin=launch.stdin,
        timeout=min(float(agent_timeout), cap),
        user=HARNESS_USER,
    )
    kill_user(HARNESS_USER)
    parsed = harness.parse(done.stdout)
    answer = paths.workdir / "answer.txt"
    answered = answer.is_file() and not answer.is_symlink() and answer.stat().st_size > 0

    # The Harness is gone: discard anything it put where the Verifier runs, then stage it.
    _clear(paths.tests, paths.logs)
    fetch_tree(f"{task_uri}tests/", paths.tests)
    verifier = task.get("verifier") or {}
    with tempfile.TemporaryDirectory(prefix="verifier-home-") as home:
        verifier_env = {
            **base_env(home),
            "PYTHONNOUSERSITE": "1",
            # test.sh may `pip install` what the image already has; never reach for PyPI
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            **{k: str(v) for k, v in (verifier.get("env") or {}).items()},
        }
        run(
            ["bash", str(paths.tests / "test.sh")],
            env=verifier_env,
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
    if verifier_reward is None:
        stop_reason = "verifier_no_result"
    elif smoke and not answered and not parsed.warning:
        # smoke proves the Harness can write an answer; a 0 from an empty answer does not
        score, stop_reason = None, "harness_no_answer"
    else:
        stop_reason = "harness_timeout" if done.returncode is None else "completed"
    out: dict[str, Any] = {
        "reward": score,
        "harness": harness.NAME,
        "metrics": {
            "tool_calls": parsed.tool_calls,
            "verifier_reward": verifier_reward,
            "correct": int(verifier_reward is not None and verifier_reward >= 1.0),
        },
        "stop_reason": stop_reason,
    }
    if parsed.warning:
        out["warning"] = parsed.warning
    return out
