#!/usr/bin/env bash
# TokWiz wrapper for OpenAI Codex CLI (`tokwiz-codex-wrapper.sh`)
# Displays pre-prompt token & cost forecast before invoking `codex`, and reconciles usage after.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKWIZ_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ $# -gt 0 && "${1:-}" != -* ]]; then
  python3 "${TOKWIZ_ROOT}/scripts/token-estimator.py" "$1" || true
fi

if command -v codex >/dev/null 2>&1; then
  codex "$@"
  EXIT_CODE=$?
else
  echo "[TokWiz] codex CLI not found in PATH; forecast completed." >&2
  EXIT_CODE=0
fi

if [[ -f "${TOKWIZ_ROOT}/hooks/post-turn-record.py" ]]; then
  echo '{}' | python3 "${TOKWIZ_ROOT}/hooks/post-turn-record.py" >/dev/null 2>&1 || true
fi

exit "${EXIT_CODE}"
