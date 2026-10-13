"""Prepare a private fixed-SHA CPU test copy; never mutate source or project env.
Usage: python3 prepare_cpu.py /tmp/tp-colocate-qa-<unique>
Downloads are preparation only; test execution uses --offline --no-sync.
"""
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
SHA = "7712ae73ce51d8076b8efa4d2e2a3b2b9aaa36d6"
source = ROOT / "data/cache/toolkit" / SHA
wheel = Path("/tmp/verlsrc/verl-0.9.0-py3-none-any.whl")
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == "6e9374db1e2ab346a0aafe9a202bee1b239a042def2f2ccd6db2a4cd04c6308b"
assert subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() == SHA
assert not subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip()
work = Path(sys.argv[1]).resolve()
work.mkdir(exist_ok=False)
copy = work / "toolkit"
copy.mkdir()
archive = work / "toolkit.tar"
with archive.open("wb") as out:
    subprocess.run(["git", "-C", str(source), "archive", SHA], stdout=out, check=True)
subprocess.run(["tar", "-xf", str(archive), "-C", str(copy)], check=True)
import os

env = {**os.environ, "UV_CACHE_DIR": str(work / "uv-cache"), "UV_PYTHON_DOWNLOADS": "never"}
subprocess.run(["uv", "sync", "--frozen", "--python", str(ROOT / "backend/.venv/bin/python"),
                "--group", "verl-ci", "--extra", "dev"], cwd=copy, env=env, check=True)
print(f"PREPARED {copy}", flush=True)
