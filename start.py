#!/usr/bin/env python3
"""Start TuningPad locally in the background (backend :8100 + vite :5180).

    ./start.py           dev: uvicorn --reload + vite dev server
    ./start.py --prod    vite build + vite preview, uvicorn without reload
    ./stop.sh            stop what this script started

PIDs (process-group leaders) and logs go to .run/.
Binding a non-loopback host requires TUNINGPAD_PASSWORD (the backend refuses otherwise).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN = ROOT / ".run"


def _spawn(name: str, cmd: list[str], cwd: Path, env: dict[str, str]) -> int:
    RUN.mkdir(exist_ok=True)
    log = (RUN / f"{name}.log").open("ab")
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
    )
    (RUN / f"{name}.pid").write_text(str(proc.pid))
    return proc.pid


def _alive(name: str) -> bool:
    pid_file = RUN / f"{name}.pid"
    if not pid_file.exists():
        return False
    try:
        os.kill(int(pid_file.read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


def _wait_http(url: str, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--prod", action="store_true", help="build the UI and run without reload")
    args = ap.parse_args()

    api_port = os.environ.get("TUNINGPAD_PORT", "8100")
    ui_port = os.environ.get("TUNINGPAD_UI_PORT", "5180")
    api_host = os.environ.get("TUNINGPAD_HOST", "127.0.0.1")
    ui_host = os.environ.get("TUNINGPAD_UI_HOST", api_host)
    env = {**os.environ, "TUNINGPAD_API": f"http://127.0.0.1:{api_port}"}

    if _alive("backend") or _alive("frontend"):
        print("already running — ./stop.sh first", file=sys.stderr)
        return 1

    uvicorn = ["uv", "run", "uvicorn", "app.main:app", "--host", api_host, "--port", api_port]
    if not args.prod:
        uvicorn.append("--reload")
    _spawn("backend", uvicorn, ROOT / "backend", env)

    if args.prod:
        subprocess.run(["npm", "run", "--silent", "build"], cwd=ROOT / "frontend", check=True)
        ui = ["npx", "vite", "preview", "--host", ui_host, "--port", ui_port, "--strictPort"]
    else:
        ui = ["npx", "vite", "--host", ui_host, "--port", ui_port, "--strictPort"]
    _spawn("frontend", ui, ROOT / "frontend", env)

    ok = _wait_http(f"http://127.0.0.1:{api_port}/api/health", 60)
    status = "up" if ok else "NOT READY: .run/backend.log"
    print(f"backend  http://{api_host}:{api_port}  {status}")
    ok_ui = _wait_http(f"http://127.0.0.1:{ui_port}/", 60)
    status = "up" if ok_ui else "NOT READY: .run/frontend.log"
    print(f"console  http://{ui_host}:{ui_port}  {status}")
    return 0 if ok and ok_ui else 1


if __name__ == "__main__":
    sys.exit(main())
