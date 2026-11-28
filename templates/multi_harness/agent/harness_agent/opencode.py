"""OpenCode, run unmodified with `opencode run` against the rollout gateway.

The gateway is an OpenAI-compatible provider ("tp") whose only model is the policy; it is
also the small model OpenCode uses for titles, so those calls are captured too. The API
key is the gateway session id. The config lives outside the Harness's HOME and is passed
with OPENCODE_CONFIG. OpenCode has no turn limit flag: the gateway's cap applies.
"""

from __future__ import annotations

import json
from pathlib import Path

from .launch import Launch, Parsed
from .procenv import base_env

NAME = "opencode"
HOME = "/home/harness"
CONFIG_PATH = Path("/etc/tuningpad/opencode.json")


def build(prompt: str, config: dict, *, region: str, max_turns: int, smoke_model: str) -> Launch:
    model = config["model_id"]
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "tp": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "TuningPad rollout gateway",
                "options": {
                    "baseURL": config["base_url"].rstrip("/"),
                    "apiKey": config.get("api_key") or "EMPTY",
                },
                "models": {model: {"name": model, "tool_call": True}},
            }
        },
        "model": f"tp/{model}",
        "small_model": f"tp/{model}",
        "autoupdate": False,
        "share": "disabled",
    }
    env = {**base_env(HOME), "OPENCODE_CONFIG": str(CONFIG_PATH)}
    cmd = ["opencode", "run", "--format", "json", "-m", f"tp/{model}", "--auto", prompt]
    return Launch(cmd=cmd, env=env, stdin="", files={CONFIG_PATH: json.dumps(cfg, indent=1)})


def parse(stdout: str) -> Parsed:
    calls = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        calls += event.get("type") == "tool_use"
    return Parsed(tool_calls=calls)
