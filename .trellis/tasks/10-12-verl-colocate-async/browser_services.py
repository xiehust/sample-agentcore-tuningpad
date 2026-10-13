"""Own only mock/Vite/raw Chrome processes, cleanly stop on TERM/INT.
python3 browser_services.py WORK
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[3]
TASK = Path(__file__).resolve().parent
WORK = Path(sys.argv[1]).resolve()
chrome = Path("/snap/chromium/current/usr/lib/chromium-browser/chrome")
assert chrome.is_file() and os.access(chrome, os.X_OK)
for port in (5187, 18187):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
home = Path.home() / "snap/chromium/common" / WORK.name
home.mkdir(exist_ok=False)
profile = home / "profile"
profile.mkdir()
(WORK / "browser-empty.json").write_text("{}")
state = {"home": str(home), "profile": str(profile), "mock": 18187, "vite": 5187}
(WORK / "browser-services.json").write_text(json.dumps(state, indent=2))
stop = threading.Event()
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: stop.set())
children = []
logs = []
try:
    commands = [
        ("mock", [sys.executable, str(TASK / "mock_api.py"), str(WORK), "18187"], ROOT, os.environ.copy()),
        ("vite", ["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "5187", "--strictPort"],
         ROOT / "frontend", {**os.environ, "TUNINGPAD_API": "http://127.0.0.1:18187"}),
        ("chrome", [str(chrome), "--headless=new", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                    f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                    "--disable-background-networking", "--disable-component-update", "--disable-sync", "about:blank"],
         WORK, {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache")}),
    ]
    for name, cmd, cwd, env in commands:
        log = (WORK / f"{name}.log").open("w")
        logs.append(log)
        child = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        children.append((name, child))
        print(name, child.pid, flush=True)
    print("Services launched; inspect own profile/DevToolsActivePort for CDP", flush=True)
    stop.wait()
finally:
    for name, child in reversed(children):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
    for name, child in reversed(children):
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        print("STOPPED", name, child.pid, child.returncode, flush=True)
    for log in logs:
        log.close()
