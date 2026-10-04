#!/usr/bin/env bash
# Canonical quality gate for TuningPad. Any failing section fails the gate.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAIL=0
section() { printf '\n════ %s ════\n' "$1"; }
result() {
  if [ "$1" -eq 0 ]; then printf '── %s: OK\n' "$2"; else printf '── %s: FAIL (exit %s)\n' "$2" "$1"; FAIL=1; fi
}

section "backend · ruff"
(cd "$ROOT/backend" && uv run ruff check . && uv run ruff format --check .); result $? "ruff"

section "backend · pytest"
(cd "$ROOT/backend" && uv run pytest -q -n auto); result $? "pytest"

section "local lifecycle · syntax"
(cd "$ROOT/backend" && uv run ruff check ../start.py && python3 -m py_compile ../start.py && bash -n ../stop.sh); result $? "lifecycle"

section "cluster assets · shell syntax"
(shopt -s nullglob; for f in "$ROOT"/cluster_assets/*.sh "$ROOT"/trainer_image/*.sh; do bash -n "$f" || exit 1; done); result $? "bash -n"

section "frontend · eslint"
(cd "$ROOT/frontend" && npm run --silent lint); result $? "eslint"

section "frontend · tsc + vite build"
(cd "$ROOT/frontend" && npm run --silent build); result $? "build"

section "i18n · key parity"
python3 "$ROOT/scripts/i18n_check.py"; result $? "i18n_check"

printf '\n════ verify: %s ════\n' "$([ $FAIL -eq 0 ] && echo PASS || echo FAIL)"
exit $FAIL
