#!/usr/bin/env bash
# Start API + trade manager on the host (no Docker). Does not start Wine/MT5.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-$ROOT/.venv}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export PYTHONPATH="$ROOT"
export EXECUTION_BACKEND="${EXECUTION_BACKEND:-mt5linux}"

API_PORT="${API_PORT:-8001}"
LOG_DIR="$ROOT/logs"
PID_DIR="$ROOT/run"
mkdir -p "$LOG_DIR" "$PID_DIR"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Virtualenv not found at $VENV — run: python -m venv .venv && pip install -r requirements.txt"
  exit 1
fi

start_api() {
  if ss -tln 2>/dev/null | grep -q ":$API_PORT " || \
     netstat -tln 2>/dev/null | grep -q ":$API_PORT "; then
    echo "Trade API already listening on $API_PORT"
    return
  fi
  echo "Starting trade API on port $API_PORT..."
  cd "$ROOT"
  nohup "$VENV/bin/uvicorn" src.api.server:app \
    --host 0.0.0.0 --port "$API_PORT" \
    >"$LOG_DIR/api.log" 2>&1 &
  echo $! > "$PID_DIR/api.pid"
  sleep 1
}

start_manager() {
  if pgrep -f "src.main" >/dev/null 2>&1; then
    echo "Trade manager already running"
    return
  fi
  echo "Starting trade manager (EXECUTION_BACKEND=$EXECUTION_BACKEND)..."
  cd "$ROOT"
  nohup "$VENV/bin/python" -m src.main \
    >"$LOG_DIR/manager.log" 2>&1 &
  echo $! > "$PID_DIR/manager.pid"
  sleep 1
}

start_api
start_manager

echo ""
echo "=== Trade system started (local, no Docker) ==="
echo "API:              http://localhost:$API_PORT"
echo "Panel:            http://localhost:$API_PORT/panel"
echo "EXECUTION_BACKEND: $EXECUTION_BACKEND"
if [[ "$EXECUTION_BACKEND" == "ea" ]]; then
  echo "EA server:        0.0.0.0:${EA_SERVER_PORT:-19520} (attach TradeIdeaExecutor in MT5)"
else
  echo "MT5 RPyC:         ${MT5_HOST:-localhost}:${MT5_PORT:-18812} (start mt5linux separately)"
fi
echo "Logs:             $LOG_DIR/"
