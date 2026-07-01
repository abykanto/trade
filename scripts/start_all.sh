#!/usr/bin/env bash
# Start MT5 terminal, mt5linux RPyC server, trade API, and trade manager.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-$ROOT/.venv}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export WINEPREFIX="${WINEPREFIX:-/home/privatecirle/.wine}"
export WINEDEBUG="${WINEDEBUG:--all}"
export PYTHONPATH="$ROOT"
export DISPLAY="${DISPLAY:-:0}"

API_PORT="${API_PORT:-8001}"
MT5_PORT="${MT5_PORT:-18812}"
LOG_DIR="$ROOT/logs"
PID_DIR="$ROOT/run"
mkdir -p "$LOG_DIR" "$PID_DIR"

WINE_PYTHON="$WINEPREFIX/drive_c/Program Files/Python312/python.exe"
MT5_TERMINAL="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"

configure_mt5_terminal() {
  echo "Configuring MT5 for algo trading (common.ini)..."
  python3 "$ROOT/scripts/configure_mt5_terminal.py"
}

start_mt5_terminal() {
  if pgrep -f "terminal64.exe" >/dev/null 2>&1; then
    echo "MT5 terminal already running (common.ini updated for next restart)"
    return
  fi
  echo "Starting MT5 terminal..."
  nohup wine "$MT5_TERMINAL" >/dev/null 2>&1 &
  echo $! > "$PID_DIR/mt5_terminal.pid"
  sleep 8
}

ensure_algo_trading() {
  echo "Verifying MT5 algo trading..."
  if "$VENV/bin/python" "$ROOT/scripts/ensure_mt5_algo_trading.py" --toggle; then
    return
  fi
  echo "Warning: could not confirm algo trading is enabled — check the green toolbar button in MT5" >&2
}

start_mt5linux_server() {
  if ss -tln | grep -q ":$MT5_PORT "; then
    echo "mt5linux server already listening on $MT5_PORT"
    return
  fi
  echo "Starting mt5linux RPyC server on port $MT5_PORT..."
  # Do not redirect stdout/stderr — Wine Python crashes with invalid stdio handles.
  wine "$WINE_PYTHON" -m mt5linux -p "$MT5_PORT" < /dev/null &
  echo $! > "$PID_DIR/mt5linux.pid"
  disown "$(cat "$PID_DIR/mt5linux.pid")" 2>/dev/null || true
  sleep 4
}

start_api() {
  if ss -tln | grep -q ":$API_PORT "; then
    echo "Trade API already listening on $API_PORT"
    return
  fi
  echo "Starting trade API on port $API_PORT..."
  cd "$ROOT"
  nohup "$VENV/bin/uvicorn" src.api.server:app \
    --host 0.0.0.0 --port "$API_PORT" \
    >"$LOG_DIR/api.log" 2>&1 &
  echo $! > "$PID_DIR/api.pid"
  sleep 2
}

start_manager() {
  if pgrep -f "src.main" >/dev/null 2>&1; then
    echo "Trade manager already running"
    return
  fi
  echo "Starting trade manager..."
  cd "$ROOT"
  export DAILY_DD_PCT="${DAILY_DD_PCT:-0.03}"
  export MAX_ACCOUNT_RISK_PCT="${MAX_ACCOUNT_RISK_PCT:-6.0}"
  nohup "$VENV/bin/python" -m src.main \
    >"$LOG_DIR/manager.log" 2>&1 &
  echo $! > "$PID_DIR/manager.pid"
  sleep 2
}

configure_mt5_terminal
start_mt5_terminal
start_mt5linux_server
ensure_algo_trading
start_api
start_manager

echo ""
echo "=== Trade system started ==="
echo "API:            http://localhost:$API_PORT"
echo "MT5 RPyC:       localhost:$MT5_PORT"
echo "Logs:           $LOG_DIR/"
echo ""
echo "Submit a signal:"
echo "  curl -X POST http://localhost:$API_PORT/signals \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"symbol\":\"EURUSD\",\"direction\":\"BUY\",\"entry_price\":1.0850,\"hard_stop\":1.0800,\"take_profit\":1.0950,\"max_idea_risk\":10.0}'"
