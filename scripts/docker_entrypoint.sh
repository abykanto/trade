#!/usr/bin/env bash
# Run API + trade manager inside Docker (EA connects from host MT5).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p data logs run

API_PORT="${API_PORT:-8001}"

echo "Starting trade API on :${API_PORT}..."
uvicorn src.api.server:app --host 0.0.0.0 --port "$API_PORT" &
API_PID=$!

echo "Starting trade manager (EXECUTION_BACKEND=${EXECUTION_BACKEND:-ea})..."
python -m src.main &
MANAGER_PID=$!

trap 'kill "$API_PID" "$MANAGER_PID" 2>/dev/null; wait' INT TERM

wait -n "$API_PID" "$MANAGER_PID"
