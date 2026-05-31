#!/usr/bin/env bash
#
# Launch the BitGN ECOM agent against the configured benchmark.
#
# Usage:
#   ./run.sh                 # run every task in the benchmark
#   ./run.sh t01             # run a single task
#   ./run.sh t01 t04 t07     # run a subset of tasks
#
# Configuration is read from `.env` (see .env.example). Override any value
# inline, e.g.:  WORKERS=8 BENCH_ID=bitgn/ecom1-prod ./run.sh
#
set -euo pipefail

# Resolve the directory of this script (project root), regardless of CWD.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$ROOT/agent"

# Activate a local virtualenv if one exists next to this script.
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  for cand in "$ROOT/venv/bin/activate" "$ROOT/.venv/bin/activate"; do
    if [[ -f "$cand" ]]; then
      # shellcheck disable=SC1090
      source "$cand"
      break
    fi
  done
fi

PY="${PYTHON:-python}"

# Sanity checks ------------------------------------------------------------
if [[ ! -f "$ROOT/.env" ]]; then
  echo "WARNING: $ROOT/.env not found. Copy .env.example to .env and fill it in." >&2
fi

if ! command -v hermes >/dev/null 2>&1 && [[ -z "${HERMES_BIN:-}" ]]; then
  echo "FATAL: 'hermes' is not on PATH. Install it with 'pip install hermes-agent'" >&2
  echo "       into the same environment as $PY, or set HERMES_BIN in .env." >&2
  exit 1
fi

echo "BENCH_ID=${BENCH_ID:-<from .env>}  MODEL_ID=${MODEL_ID:-<default>}  WORKERS=${WORKERS:-4}"

# main.py loads .env itself; running from agent/ keeps its relative imports
# (env_loader, prompts, hermes_home/) resolvable.
cd "$AGENT_DIR"
exec "$PY" -m main "$@"
