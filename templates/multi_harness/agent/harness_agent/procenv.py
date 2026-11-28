"""The minimal environment every child process starts from (no AWS credentials)."""

from __future__ import annotations

import os


def base_env(home: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "LANG": "C.UTF-8",
    }
