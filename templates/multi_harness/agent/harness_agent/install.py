"""Image build step: install the Harnesses selected in tp_config.json, at pinned versions.

Run once by the Dockerfile (with network access); nothing is installed at rollout time.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

VERSIONS = {
    "claude_code": "2.1.332",
    "opencode": "1.19.6",
    "mini_swe_agent": "2.4.6",
}


def commands(selected: list[str]) -> list[list[str]]:
    unknown = sorted(set(selected) - set(VERSIONS))
    if unknown or not selected:
        raise ValueError(f"unknown or no harnesses: {unknown or selected}")
    npm = ["npm", "install", "-g", "--no-fund", "--no-audit"]
    out: list[list[str]] = []
    if "claude_code" in selected:
        out += [
            npm + [f"@anthropic-ai/claude-code@{VERSIONS['claude_code']}"],
            ["claude", "--version"],
        ]
    if "opencode" in selected:
        out += [npm + [f"opencode-ai@{VERSIONS['opencode']}"], ["opencode", "--version"]]
    if "mini_swe_agent" in selected:
        out += [
            [sys.executable, "-m", "venv", "/opt/mini"],
            ["/opt/mini/bin/pip", "install", f"mini-swe-agent=={VERSIONS['mini_swe_agent']}"],
            ["/opt/mini/bin/mini", "--help"],
        ]
    return out


def main(config_path: str) -> None:
    selected = json.loads(Path(config_path).read_text())["harnesses"]
    for cmd in commands(selected):
        print("+", " ".join(cmd), flush=True)
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL if cmd[-1] == "--help" else None)


if __name__ == "__main__":
    main(sys.argv[1])
