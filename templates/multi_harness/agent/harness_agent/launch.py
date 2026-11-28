"""What every Harness module returns: how to start it, and what it reported."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Launch:
    cmd: list[str]
    env: dict[str, str]
    stdin: str
    # written (by the agent, readable by the Harness) before the Harness starts
    files: dict[Path, str] = field(default_factory=dict)
    # read after the Harness's processes are killed, instead of its stdout
    output_file: Path | None = None


@dataclass(frozen=True)
class Parsed:
    tool_calls: int
    warning: str | None = None
