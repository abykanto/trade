#!/usr/bin/env bash
# Local dev with MQL5 EA execution (no Docker, no mt5linux).
set -euo pipefail

export EXECUTION_BACKEND=ea
export EA_SERVER_HOST="${EA_SERVER_HOST:-0.0.0.0}"
export EA_SERVER_PORT="${EA_SERVER_PORT:-19520}"

exec "$(dirname "$0")/start_local.sh"
