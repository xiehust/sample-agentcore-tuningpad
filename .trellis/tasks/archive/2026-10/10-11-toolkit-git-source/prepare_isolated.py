"""Prepare a no-sibling verification copy; never mutate the source tree or install.

Run from any cwd with Python 3.12. The printed temporary parent is intentionally
retained for the parent agent's installation, offline tests, and final make verify.
Only this task's necessary untracked product/test files are copied explicitly.
Existing frontend dependencies are copied, not installed or upgraded.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PREFIXES = (
    "backend/app/",
    "backend/tests/",
    "templates/",
    "cluster_assets/",
    "trainer_image/",
    "frontend/",
)
FILES = {
    "backend/pyproject.toml",
    "backend/uv.lock",
    "config/tuningpad.example.yaml",
    "scripts/verify.sh",
    "scripts/i18n_check.py",
    "Makefile",
    "start.py",
    "stop.sh",
}
NEW_FILES = {
    "backend/app/services/toolkit_source.py",
    "backend/tests/test_toolkit_source.py",
    "backend/tests/test_toolkit_consumers.py",
    "backend/tests/test_trainer_image.py",
}


def main() -> None:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    paths = sorted({p for p in tracked if p in FILES or p.startswith(PREFIXES)} | NEW_FILES)
    assert all((ROOT / p).is_file() and not (ROOT / p).is_symlink() for p in paths)
    assert (ROOT / "frontend/node_modules").is_dir()
    parent = Path(tempfile.mkdtemp(prefix="tuningpad-no-sibling."))
    project = parent / "project"
    for relative in paths:
        src, dest = ROOT / relative, project / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    shutil.copytree(
        ROOT / "frontend/node_modules", project / "frontend/node_modules", symlinks=True
    )
    (parent / "data").mkdir()
    (parent / "config").mkdir()
    (parent / "config/empty.yaml").write_text("{}\n")
    assert not (parent / "agentcore-rl-toolkit").exists()
    assert not (project / "backend/.venv").exists()
    assert not (project / "config/tuningpad.yaml").exists()

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    evidence = {
        "parent": str(parent),
        "project": str(project),
        "venv": str(parent / "venv"),
        "config": str(parent / "config/empty.yaml"),
        "data": str(parent / "data"),
        "source_sha256": {p: digest(ROOT / p) for p in paths},
    }
    assert all(digest(project / p) == h for p, h in evidence["source_sha256"].items())
    (parent / "copy-manifest.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({k: v for k, v in evidence.items() if k != "source_sha256"}, indent=2))
    print(f"Copied {len(paths)} source/fixture files; lock unchanged; no sibling checkout.")


if __name__ == "__main__":
    main()
