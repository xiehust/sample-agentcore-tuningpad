#!/usr/bin/env bash
# Stop the processes started by ./start.py (process groups recorded in .run/).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in "$ROOT"/.run/*.pid; do
  [ -f "$f" ] || continue
  pid=$(cat "$f")
  if kill -0 "$pid" 2>/dev/null; then kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid"; echo "stopped $(basename "$f" .pid) ($pid)"; fi
  rm -f "$f"
done
