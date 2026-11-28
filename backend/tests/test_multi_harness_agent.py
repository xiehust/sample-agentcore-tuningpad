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

from harness_agent import (  # noqa: E402
    claude_code,
    install,
    mini_swe_agent,
    opencode,
    reward,
    rollout,
)

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
        (0.5, 1, 0.0),  # not correct counts as wrong
        (None, 3, None),  # Verifier produced nothing: unscorable, not wrong
    ],
)
def test_reward_adds_a_tool_efficiency_bonus_only_to_correct_answers(verifier, calls, expected):
    got = reward.score(verifier, calls, cap=0.1, budget=40)
    assert got == (pytest.approx(expected) if expected is not None else None)


# ---------------- S9: Claude Code launch ----------------


SMOKE_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_claude_code_talks_to_the_gateway_with_the_session_key():
    launch = claude_code.build(
        "Solve it", GATEWAY, region="us-east-1", max_turns=200, smoke_model=SMOKE_MODEL
    )
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


def test_claude_code_smoke_uses_the_configured_bedrock_model(monkeypatch):
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://169.254.170.2/creds")
    cfg = {**GATEWAY, "smoke": True, "model_id": "openai.gpt-oss-20b-1:0"}
    env = claude_code.build("q", cfg, region="eu-west-1", max_turns=5, smoke_model=SMOKE_MODEL).env
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_REGION"] == "eu-west-1"
    assert env["ANTHROPIC_MODEL"] == SMOKE_MODEL
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


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 400 Model access is denied for this account",
        "API Error (403): User is not authorized to perform: bedrock:InvokeModel",
        "ValidationException: The provided model identifier is invalid.",
    ],
)
def test_other_bedrock_access_errors_are_warnings_too(text):
    out = _stream({"type": "result", "is_error": True, "result": text})
    assert claude_code.parse(out).warning


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


PARAMS = {
    "tool_bonus_cap": 0.1,
    "tool_call_budget": 40,
    "harness_max_turns": 200,
    "smoke_bedrock_model": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}


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

    def __init__(
        self,
        paths: rollout.Paths,
        *,
        verifier_reward: str | None = "1.0",
        verifier_json: str | None = None,
        tamper=False,
        answer=True,
        stream=None,
    ):
        self.paths = paths
        self.verifier_reward = verifier_reward
        self.verifier_json = verifier_json
        self.tamper = tamper
        self.answer = answer
        self.stream = stream
        self.calls: list[dict] = []
        self.killed: list[str] = []

    def kill_user(self, user: str) -> None:
        self.killed.append(user)
        self.calls.append({"killed": user})

    def __call__(self, cmd, *, env, cwd, stdin, timeout, user=None):
        seen = {
            "cmd": cmd,
            "env": env,
            "cwd": cwd,
            "timeout": timeout,
            "user": user,
            "inputs": sorted(p.name for p in self.paths.inputs.iterdir()),
            "tests_visible": self.paths.tests.exists() and any(self.paths.tests.iterdir()),
        }
        self.calls.append(seen)
        if user is not None:  # the Harness runs as its own user; the Verifier does not
            if self.answer:
                (self.paths.workdir / "answer.txt").write_text("42")
            if self.tamper:  # a reward hack: pre-write the Verifier's output and a test
                (self.paths.logs / "verifier").mkdir(parents=True, exist_ok=True)
                (self.paths.logs / "verifier" / "reward.txt").write_text("1.0")
                (self.paths.tests / "test.sh").write_text("exit 0\n")
            stream = self.stream or _stream(_tool(), _tool(), {"type": "result"})
            return rollout.Completed(0, stream, "")
        seen["tests_listing"] = sorted(p.name for p in self.paths.tests.iterdir())
        seen["test_sh"] = (self.paths.tests / "test.sh").read_text()
        (self.paths.logs / "verifier").mkdir(parents=True, exist_ok=True)
        if self.verifier_reward is not None:
            (self.paths.logs / "verifier" / "reward.txt").write_text(self.verifier_reward)
        if self.verifier_json is not None:
            (self.paths.logs / "verifier" / "reward.json").write_text(self.verifier_json)
        return rollout.Completed(0, "", "")


def test_a_rollout_runs_the_harness_then_the_verifier(world):
    procs = FakeProcesses(world["paths"])
    out = rollout.run_rollout(
        world["payload"],
        params=PARAMS,
        paths=world["paths"],
        read=world["read"],
        fetch_tree=world["fetch_tree"],
        run=procs,
        kill_user=procs.kill_user,
        region="us-east-1",
    )
    harness, killed, verifier = procs.calls
    assert harness["cmd"][0] == "claude" and harness["inputs"] == ["data.csv"]
    # the Harness runs as its own user; whatever it left running is killed before grading
    assert harness["user"] == rollout.HARNESS_USER and verifier["user"] is None
    assert killed == {"killed": rollout.HARNESS_USER}
    # the Verifier gets a fresh HOME, so nothing the Harness planted in its HOME loads
    assert verifier["env"]["HOME"] != harness["env"]["HOME"]
    assert verifier["env"]["PYTHONNOUSERSITE"] == "1"
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
        params=PARAMS,
        paths=world["paths"],
        read=world["read"],
        fetch_tree=world["fetch_tree"],
        run=(p := FakeProcesses(world["paths"], verifier_reward=None)),
        kill_user=p.kill_user,
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
        params=PARAMS,
        paths=paths,
        read=world["read"],
        fetch_tree=world["fetch_tree"],
        run=procs,
        kill_user=procs.kill_user,
        region="us-east-1",
    )
    assert procs.calls[0]["inputs"] == ["data.csv"]
    assert not (paths.workdir / "stale").exists()


def test_a_payload_naming_an_uninstalled_harness_fails(world):
    with pytest.raises(rollout.HarnessUnavailable):
        rollout.run_rollout(
            {**world["payload"], "harness": "codex"},
            params=PARAMS,
            paths=world["paths"],
            read=world["read"],
            fetch_tree=world["fetch_tree"],
            run=(p := FakeProcesses(world["paths"])),
            kill_user=p.kill_user,
            region="us-east-1",
        )


def _run(world, procs, payload=None):
    return rollout.run_rollout(
        payload or world["payload"],
        params=PARAMS,
        paths=world["paths"],
        read=world["read"],
        fetch_tree=world["fetch_tree"],
        run=procs,
        kill_user=procs.kill_user,
        region="us-east-1",
    )


def test_what_the_harness_writes_into_the_verifier_dirs_is_discarded(world):
    procs = FakeProcesses(world["paths"], tamper=True, verifier_reward=None)
    out = _run(world, procs)
    verifier = procs.calls[-1]
    assert verifier["test_sh"] == "#!/bin/sh\n"  # the Task's own test.sh, not the planted one
    assert out["reward"] is None  # the planted reward.txt was cleared before grading


def test_smoke_without_an_answer_fails_unless_the_model_was_unavailable(world):
    smoke = {**world["payload"], "_config": {**GATEWAY, "smoke": True}}
    out = _run(world, FakeProcesses(world["paths"], answer=False, verifier_reward="0.0"), smoke)
    assert out["reward"] is None and out["stop_reason"] == "harness_no_answer"

    denied = _stream({"type": "result", "is_error": True, "result": "403 AccessDeniedException"})
    procs = FakeProcesses(world["paths"], answer=False, verifier_reward="0.0", stream=denied)
    out = _run(world, procs, smoke)
    assert out["reward"] == 0.0 and out["warning"]


def test_smoke_caps_the_harness_time_below_the_smoke_timeout(world):
    smoke = {**world["payload"], "_config": {**GATEWAY, "smoke": True}}
    procs = FakeProcesses(world["paths"])
    _run(world, procs, smoke)
    assert procs.calls[0]["timeout"] == min(60.0, rollout.SMOKE_HARNESS_TIMEOUT_S)


def test_a_harness_killed_at_the_timeout_takes_its_children_with_it(tmp_path):
    import time

    start = time.monotonic()
    done = rollout.run_process(
        ["sh", "-c", "sleep 30 & sleep 30"],
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        stdin="",
        timeout=0.5,
    )
    assert done.returncode is None and time.monotonic() - start < 10


@pytest.mark.parametrize(
    ("reward_json", "expected"),
    [
        # SmolDataEnvs multi-reward Verifiers: correctness is the training signal
        ('{"correctness": 1.0, "submission": 1.0, "tool_efficiency": 0.4}', 1.0),
        ('{"correctness": 0.0, "submission": 1.0, "tool_efficiency": null}', 0.0),
        ('{"reward": 1.0, "other": 0.0}', 1.0),
        ('{"score": 1.0}', 1.0),  # a single score is used whatever its name
        ('{"a": 1.0, "b": 0.0}', None),  # ambiguous: never combine scores
        ("not json", None),
    ],
)
def test_a_multi_reward_verifier_is_read_from_reward_json(world, reward_json, expected):
    procs = FakeProcesses(world["paths"], verifier_reward=None, verifier_json=reward_json)
    out = _run(world, procs)
    assert out["metrics"].get("verifier_reward") == expected


def test_metrics_are_all_numbers_even_without_a_verifier_result(world):
    """The toolkit's rollout result schema takes float metrics only."""
    out = _run(world, FakeProcesses(world["paths"], verifier_reward=None))
    assert all(isinstance(v, int | float) for v in out["metrics"].values())
    assert "verifier_reward" not in out["metrics"]


# ---------------- S17: OpenCode and Mini-SWE-Agent ----------------


def test_opencode_runs_against_the_gateway_with_a_written_config():
    launch = opencode.build("Solve it", GATEWAY, region="us-east-1", max_turns=200, smoke_model="x")
    cfg = json.loads(launch.files[opencode.CONFIG_PATH])
    provider = cfg["provider"]["tp"]
    assert provider["options"] == {"baseURL": "http://10.0.0.5:18765/v1", "apiKey": "sid-123"}
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert "Qwen/Qwen3.5-4B" in provider["models"]
    # the title / small-model calls go to the policy too
    assert cfg["model"] == cfg["small_model"] == "tp/Qwen/Qwen3.5-4B"
    assert cfg["autoupdate"] is False and cfg["share"] == "disabled"
    assert launch.env["OPENCODE_CONFIG"] == str(opencode.CONFIG_PATH)
    assert not any(k.startswith("AWS_") for k in launch.env)
    assert launch.cmd[:2] == ["opencode", "run"] and launch.cmd[-1] == "Solve it"
    assert launch.cmd[launch.cmd.index("--format") + 1] == "json"
    assert launch.cmd[launch.cmd.index("-m") + 1] == "tp/Qwen/Qwen3.5-4B"


def test_opencode_counts_tool_use_events():
    out = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "step_start"},
            {"type": "tool_use", "part": {"tool": "bash"}},
            {"type": "tool_use", "part": {"tool": "read"}},
            {"type": "text", "part": {"text": "done"}},
            {"type": "step_finish"},
        ]
    )
    assert opencode.parse(out).tool_calls == 2


def test_mini_swe_agent_runs_against_the_gateway():
    launch = mini_swe_agent.build(
        "Solve it", GATEWAY, region="us-east-1", max_turns=50, smoke_model="x"
    )
    cmd = launch.cmd
    assert cmd[0] == mini_swe_agent.MINI_BIN
    assert {"-y", "--exit-immediately"} <= set(cmd)
    assert cmd[cmd.index("-l") + 1] == "0"  # no cost limit (self-hosted policy)
    assert cmd[cmd.index("-m") + 1] == "openai/Qwen/Qwen3.5-4B"
    assert cmd[cmd.index("-t") + 1] == "Solve it"
    configs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-c"]
    assert configs[0] == "mini.yaml"
    assert "model.model_kwargs.api_base=http://10.0.0.5:18765/v1" in configs
    assert "model.model_kwargs.api_key=sid-123" in configs
    assert "agent.step_limit=50" in configs
    assert cmd[cmd.index("-o") + 1] == str(launch.output_file)
    assert launch.env["MSWEA_COST_TRACKING"] == "ignore_errors"
    assert launch.env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"  # no network fetch


def test_mini_swe_agent_counts_tool_calls_in_its_trajectory():
    traj = {
        "info": {"exit_status": "Submitted"},
        "messages": [
            {"role": "system"},
            {"role": "user"},
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
            {"role": "tool"},
            {"role": "assistant", "tool_calls": [{"id": "2"}, {"id": "3"}]},
            {"role": "tool"},
            {"role": "exit"},
        ],
    }
    assert mini_swe_agent.parse(json.dumps(traj)).tool_calls == 3
    assert mini_swe_agent.parse("").tool_calls == 0  # no trajectory written


# ---------------- S18: harness selection and files ----------------


def _harness_run(world, harness, params=None):
    procs = FakeProcesses(world["paths"])
    written = {}

    def write_file(path, content):
        written[path] = content

    out = rollout.run_rollout(
        {**world["payload"], "harness": harness},
        params={
            **PARAMS,
            "harnesses": ["claude_code", "opencode", "mini_swe_agent"],
            **(params or {}),
        },
        paths=world["paths"],
        read=world["read"],
        fetch_tree=world["fetch_tree"],
        run=procs,
        kill_user=procs.kill_user,
        region="us-east-1",
        write_file=write_file,
        read_output=lambda path: json.dumps(
            {"messages": [{"role": "assistant", "tool_calls": [{}]}]}
        ),
    )
    return out, procs, written


def test_the_payload_picks_the_harness(world):
    out, procs, written = _harness_run(world, "opencode")
    assert procs.calls[0]["cmd"][:2] == ["opencode", "run"] and out["harness"] == "opencode"
    assert opencode.CONFIG_PATH in written
    out, procs, _ = _harness_run(world, "mini_swe_agent")
    assert procs.calls[0]["cmd"][0] == mini_swe_agent.MINI_BIN
    assert out["metrics"]["tool_calls"] == 1  # read from the trajectory after the kill


def test_a_harness_not_in_this_image_is_refused(world):
    with pytest.raises(rollout.HarnessUnavailable):
        _harness_run(world, "opencode", params={"harnesses": ["claude_code"]})


# ---------------- S20: image build ----------------


def test_install_commands_follow_the_selected_harnesses():
    only_cc = install.commands(["claude_code"])
    assert any("@anthropic-ai/claude-code@" in " ".join(c) for c in only_cc)
    assert not any("opencode-ai" in " ".join(c) or "mini-swe-agent" in " ".join(c) for c in only_cc)
    all3 = " | ".join(
        " ".join(c) for c in install.commands(["claude_code", "opencode", "mini_swe_agent"])
    )
    assert f"opencode-ai@{install.VERSIONS['opencode']}" in all3
    assert f"mini-swe-agent=={install.VERSIONS['mini_swe_agent']}" in all3
    assert "/opt/mini" in all3  # its own venv, away from the Task environment
    with pytest.raises(ValueError):
        install.commands(["codex"])
