"""agent-browser wrapper: isolated session, explicit own loopback CDP, JSON transcript.
Usage: python3 browser_cli.py WORK <agent-browser arguments>
"""
import json
import os
from pathlib import Path
import subprocess
import sys

work = Path(sys.argv[1]).resolve()
state = json.loads((work / "browser-services.json").read_text())
port = int((Path(state["profile"]) / "DevToolsActivePort").read_text().splitlines()[0])
env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BROWSER_")}
args = ["agent-browser", "--session", work.name, "--config", str(work / "browser-empty.json"),
        "--cdp", f"http://127.0.0.1:{port}", *sys.argv[2:]]
result = subprocess.run(args, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
with (work / "browser-transcript.jsonl").open("a") as log:
    log.write(json.dumps({"args": sys.argv[2:], "stdout": result.stdout, "stderr": result.stderr,
                          "returncode": result.returncode}, ensure_ascii=False) + "\n")
print(result.stdout, end="")
print(result.stderr, end="", file=sys.stderr)
raise SystemExit(result.returncode)
