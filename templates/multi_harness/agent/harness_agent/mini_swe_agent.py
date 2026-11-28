"""Mini-SWE-Agent (v2, one bash tool), run unmodified with `mini` against the gateway.

It is installed in its own venv (/opt/mini) so its dependencies stay out of the Task
environment; the commands it runs still use the system Python. It reports through its
trajectory file, read after its processes are killed.
"""

from __future__ import annotations

import json
from pathlib import Path

from .launch import Launch, Parsed
from .procenv import base_env

NAME = "mini_swe_agent"
HOME = "/home/harness"
MINI_BIN = "/opt/mini/bin/mini"
TRAJECTORY = Path(HOME) / "mini_trajectory.json"


def build(prompt: str, config: dict, *, region: str, max_turns: int, smoke_model: str) -> Launch:
    env = {
        **base_env(HOME),
        "MSWEA_CONFIGURED": "true",
        "MSWEA_COST_TRACKING": "ignore_errors",  # a self-hosted policy has no price
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",  # do not fetch the cost map
    }
    cmd = [
        MINI_BIN,
        "-y",
        "--exit-immediately",
        "-l",
        "0",
        "-m",
        f"openai/{config['model_id']}",
        "-t",
        prompt,
        "-c",
        "mini.yaml",
        "-c",
        f"model.model_kwargs.api_base={config['base_url'].rstrip('/')}",
        "-c",
        f"model.model_kwargs.api_key={config.get('api_key') or 'EMPTY'}",
        "-c",
        f"agent.step_limit={max_turns}",
        "-o",
        str(TRAJECTORY),
    ]
    return Launch(cmd=cmd, env=env, stdin="", output_file=TRAJECTORY)


def parse(trajectory: str) -> Parsed:
    try:
        messages = json.loads(trajectory).get("messages") or []
    except (ValueError, AttributeError):
        return Parsed(tool_calls=0)
    calls = sum(
        len(m.get("tool_calls") or [])
        for m in messages
        if isinstance(m, dict) and m.get("role") == "assistant"
    )
    return Parsed(tool_calls=calls)
