"""The multi_harness Agent's rollout logic (templates/multi_harness/agent/harness_agent).

These modules run inside the Agent image; they do not import bedrock_agentcore, so the
backend suite can exercise them with a fake harness process and local "S3" directories.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.core.config import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "templates" / "multi_harness" / "agent"))

from harness_agent import claude_code, reward, rollout  # noqa: E402

GATEWAY = {
    "base_url": "http://10.0.0.5:18765/v1",
    "model_id": "Qwen/Qwen3.5-4B",
    "api_key": "sid-123",
    "sampling_params": {},
}

# ---------------- S7: reward ----------------


@pytest.mark.parametrize(
    ("verifier", "calls", "expected"),
    [
        (1.0, 0, 1.1),  # correct, no tool calls: full bonus
        (1.0, 10, 1.075),  # 0.1 * (1 - 10/40)
        (1.0, 40, 1.0),  # at the budget: no bonus
        (1.0, 90, 1.0),  # past the budget: still no bonus, never negative
        (0.0, 1, 0.0),  # wrong: no bonus, so giving up early earns nothing
        (None, 3, None),  # Verifier produced nothing: unscorable, not wrong
    ],
)
def test_reward_adds_a_tool_efficiency_bonus_only_to_correct_answers(verifier, calls, expected):
    got = reward.score(verifier, calls, cap=0.1, budget=40)
    assert got == (pytest.approx(expected) if expected is not None else None)


# ---------------- S9: Claude Code launch ----------------


def test_claude_code_talks_to_the_gateway_with_the_session_key():
    launch = claude_code.build("Solve it", GATEWAY, region="us-east-1", max_turns=200)
    env = launch.env
    # Claude Code appends /v1/messages itself
    assert env["ANTHROPIC_BASE_URL"] == "http://10.0.0.5:18765"
    assert env["ANTHROPIC_API_KEY"] == "sid-123"
    # every model alias, including the small/fast one, is the policy being trained
    for k in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert env[k] == "Qwen/Qwen3.5-4B"
    for k in (
        "DISABLE_AUTOUPDATER",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ):
        assert env[k] == "1"
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert not any(k.startswith("AWS_") for k in env)  # no credentials reach the harness
    assert launch.stdin == "Solve it"
    assert launch.cmd[:2] == ["claude", "-p"]
    assert "--dangerously-skip-permissions" in launch.cmd
    assert launch.cmd[launch.cmd.index("--max-turns") + 1] == "200"


def test_claude_code_smoke_uses_bedrock_haiku_for_the_region(monkeypatch):
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://169.254.170.2/creds")
    cfg = {**GATEWAY, "smoke": True, "model_id": "openai.gpt-oss-20b-1:0"}
    env = claude_code.build("q", cfg, region="eu-west-1", max_turns=5).env
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_REGION"] == "eu-west-1"
    assert env["ANTHROPIC_MODEL"] == f"eu.{claude_code.SMOKE_BEDROCK_MODEL}"
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_API_KEY" not in env
    # Bedrock needs the runtime role's credentials, so smoke passes them through
    assert env["AWS_CONTAINER_CREDENTIALS_FULL_URI"] == "http://169.254.170.2/creds"


# ---------------- S10: stream-json ----------------


def _stream(*events) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _tool(name="Bash", parent=None):
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]},
    }


def test_stream_parser_counts_every_tool_call_including_subagents():
    out = _stream(
        {"type": "system", "subtype": "init"},
        _tool("Bash"),
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "two at once"},
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {"type": "tool_use", "name": "Task", "input": {}},
                ]
            },
        },
        _tool("Bash", parent="toolu_task"),  # inside the subagent
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 4},
        "not json",
    )
    parsed = claude_code.parse(out)
    assert parsed.tool_calls == 4 and parsed.warning is None


def test_stream_parser_turns_a_bedrock_access_error_into_a_warning():
    out = _stream(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "API Error: 403 AccessDeniedException: You don't have access to the model",
        }
    )
    parsed = claude_code.parse(out)
    assert parsed.tool_calls == 0
    assert parsed.warning and "access" in parsed.warning.lower()


# ---------------- S8: one rollout ----------------

TASK_TOML = """
[environment.env]
BUCKET_PREFIX = "owner__ds"
[verifier]
timeout_sec = 30.0
[verifier.env]
EXPECTED_ANSWER = "42"
[agent]
timeout_sec = 60.0
"""


@pytest.fixture
def world(tmp_path):
    """Local stand-ins for S3 and the container's fixed paths."""
    s3 = tmp_path / "s3"
    task = s3 / "tasks" / "t1"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("What is the answer?")
    (task / "task.toml").write_text(TASK_TOML)
    (task / "tests" / "test.sh").write_text("#!/bin/sh\n")
    (task / "tests" / "grader.py").write_text("print(1.0)\n")
    inputs = s3 / "inputs" / "owner__ds"
    inputs.mkdir(parents=True)
    (inputs / "data.csv").write_text("a\n42\n")
    paths = rollout.Paths(
        workdir=tmp_path / "workdir",
        inputs=tmp_path / "home" / "input",
        tests=tmp_path / "tests",
        logs=tmp_path / "logs",
    )

    def store(uri: str) -> Path:
        assert uri.startswith("s3://bkt/")
        return s3 / uri.removeprefix("s3://bkt/")

    def read(uri: str) -> bytes:
        return store(uri).read_bytes()

    def fetch_tree(uri: str, dest: Path) -> None:
        src = store(uri)
        for f in src.rglob("*"):
            if f.is_file():
                (dest / f.relative_to(src)).parent.mkdir(parents=True, exist_ok=True)
                (dest / f.relative_to(src)).write_bytes(f.read_bytes())

    payload = {
        "task_id": "t1",
        "task_uri": "s3://bkt/tasks/t1/",
        "input_uri": "s3://bkt/inputs/owner__ds/",
        "_config": GATEWAY,
    }
    return {"paths": paths, "read": read, "fetch_tree": fetch_tree, "payload": payload}


class FakeProcesses:
    """Plays the harness and the Verifier; records what each could see."""

    def __init__(self, paths: rollout.Paths, *, verifier_reward: str | None = "1.0"):
        self.paths = paths
        self.verifier_reward = verifier_reward
        self.calls: list[dict] = []

    def __call__(self, cmd, *, env, cwd, stdin, timeout):
        seen = {
            "cmd": cmd,
            "env": env,
            "cwd": cwd,
            "timeout": timeout,
            "inputs": sorted(p.name for p in self.paths.inputs.iterdir()),
            "tests_visible": self.paths.tests.exists() and any(self.paths.tests.iterdir()),
        }
        self.calls.append(seen)
        if cmd[0] == "claude":
            (self.paths.workdir / "answer.txt").write_text("42")
            return rollout.Completed(0, _stream(_tool(), _tool(), {"type": "result"}), "")
        if self.verifier_reward is not None:
            (self.paths.logs / "verifier").mkdir(parents=True, exist_ok=True)
            (self.paths.logs / "verifier" / "reward.txt").write_text(self.verifier_reward)
        return rollout.Completed(0, "", "")


def test_a_rollout_runs_the_harness_then_the_verifier(world):
    procs = FakeProcesses(world["paths"])
    out = rollout.run_rollout(
        world["payload"],
        params={"tool_bonus_cap": 0.1, "tool_call_budget": 40, "harness_max_turns": 200},
        paths=world["paths"],
        read=world["read"],
        fetch_tree=world["fetch_tree"],
        run=procs,
        region="us-east-1",
    )
    harness, verifier = procs.calls
    assert harness["cmd"][0] == "claude" and harness["inputs"] == ["data.csv"]
    assert harness["cwd"] == world["paths"].workdir
    assert harness["timeout"] == 60.0  # the Task's own agent timeout
    # the Verifier is staged only after the harness exits
    assert not harness["tests_visible"] and verifier["tests_visible"]
    assert verifier["env"]["EXPECTED_ANSWER"] == "42"
    assert verifier["env"]["PIP_NO_INDEX"] == "1"
    assert verifier["timeout"] == 30.0
    assert "EXPECTED_ANSWER" not in harness["env"]
    assert out["reward"] == pytest.approx(1.0 + 0.1 * (1 - 2 / 40))
    assert out["metrics"] == {"tool_calls": 2, "verifier_reward": 1.0, "correct": 1}
    assert out["harness"] == "claude_code"


def test_a_rollout_without_a_verifier_result_is_unscorable(world):
    out = rollout.run_rollout(
        world["payload"],
        params={"tool_bonus_cap": 0.1, "tool_call_budget": 40, "harness_max_turns": 200},
        paths=world["paths"],
        read=world["read"],
        fetch_tree=world["fetch_tree"],
        run=FakeProcesses(world["paths"], verifier_reward=None),
        region="us-east-1",
    )
    assert out["reward"] is None and out["stop_reason"] == "verifier_no_result"


def test_a_rollout_starts_from_clean_directories(world):
    paths = world["paths"]
    for d in (paths.workdir, paths.inputs, paths.tests, paths.logs):
        d.mkdir(parents=True, exist_ok=True)
        (d / "stale").write_text("from an earlier rollout")
    procs = FakeProcesses(paths)
    rollout.run_rollout(
        world["payload"],
        params={"tool_bonus_cap": 0.1, "tool_call_budget": 40, "harness_max_turns": 200},
        paths=paths,
        read=world["read"],
        fetch_tree=world["fetch_tree"],
        run=procs,
        region="us-east-1",
    )
    assert procs.calls[0]["inputs"] == ["data.csv"]
    assert not (paths.workdir / "stale").exists()


def test_a_payload_naming_an_uninstalled_harness_fails(world):
    with pytest.raises(rollout.HarnessUnavailable):
        rollout.run_rollout(
            {**world["payload"], "harness": "codex"},
            params={"tool_bonus_cap": 0.1, "tool_call_budget": 40, "harness_max_turns": 200},
            paths=world["paths"],
            read=world["read"],
            fetch_tree=world["fetch_tree"],
            run=FakeProcesses(world["paths"]),
            region="us-east-1",
        )
