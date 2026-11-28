"""Claude Code, run unmodified in print mode against the rollout gateway.

Training: the gateway speaks Anthropic Messages at <base_url without /v1>/v1/messages and
keys the session off the API key. Every model alias points at the policy being trained,
so the small/fast-model calls Claude Code makes are captured (and trained) too.

Smoke: TuningPad's smoke test hands out a Bedrock OpenAI endpoint Claude Code cannot use,
so it switches to Claude Haiku on Bedrock with the runtime role's credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

NAME = "claude_code"
SMOKE_BEDROCK_MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"
_MODEL_VARS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
_QUIET = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
_ACCESS_ERRORS = ("accessdenied", "don't have access", "not authorized", "is not available")


@dataclass(frozen=True)
class Launch:
    cmd: list[str]
    env: dict[str, str]
    stdin: str


@dataclass(frozen=True)
class Parsed:
    tool_calls: int
    warning: str | None


def _bedrock_profile(region: str) -> str:
    prefix = "eu" if region.startswith("eu-") else "apac" if region.startswith("ap-") else "us"
    return f"{prefix}.{SMOKE_BEDROCK_MODEL}"


def build(prompt: str, config: dict, *, region: str, max_turns: int) -> Launch:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/home/user"),
        "LANG": "C.UTF-8",
        **_QUIET,
    }
    if config.get("smoke"):
        model = _bedrock_profile(region)
        env |= {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": region}
        env |= {k: v for k, v in os.environ.items() if k.startswith("AWS_") and k != "AWS_REGION"}
    else:
        model = config["model_id"]
        env |= {
            "ANTHROPIC_BASE_URL": config["base_url"].rstrip("/").removesuffix("/v1"),
            "ANTHROPIC_API_KEY": config.get("api_key") or "EMPTY",
        }
    env |= {k: model for k in _MODEL_VARS}
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(max_turns),
        "--dangerously-skip-permissions",
    ]
    return Launch(cmd=cmd, env=env, stdin=prompt)


def parse(stdout: str) -> Parsed:
    """Tool calls (every tool_use block, subagents included) and any model-access error."""
    tool_calls = 0
    warning = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant":
            blocks = (event.get("message") or {}).get("content") or []
            tool_calls += sum(
                1 for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"
            )
        elif event.get("type") == "result" and event.get("is_error"):
            text = str(event.get("result") or "")
            if any(m in text.lower() for m in _ACCESS_ERRORS):
                warning = f"model access denied: {text[:300]}"
    return Parsed(tool_calls=tool_calls, warning=warning)
