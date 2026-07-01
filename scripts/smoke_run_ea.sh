#!/usr/bin/env bash
# Compile EA, start MT5 + Python (EA backend), verify connectivity.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-$ROOT/.venv}"
LOG_DIR=""
PID_DIR=""
API_PORT="${API_PORT:-8001}"
EA_PORT="${EA_SERVER_PORT:-19520}"
WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
MT5_TERMINAL="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

# shellcheck disable=SC1091
source "$ROOT/scripts/runtime_paths.sh"

export PYTHONPATH="$ROOT"
export DISPLAY="${DISPLAY:-:0}"
export EXECUTION_BACKEND=ea
export EA_SERVER_HOST=0.0.0.0
export EA_SERVER_PORT="$EA_PORT"
export WINEDEBUG="${WINEDEBUG:--all}"

echo "=== 1/6 Compile EA ==="
bash "$ROOT/scripts/sync_compile_ea.sh"

echo "=== 2/6 Configure MT5 (algo trading + EA socket allowlist) ==="
bash "$ROOT/scripts/configure_mt5_terminal.sh" --ea-sockets

echo "=== 3/6 Install chart profile with EA ==="
bash "$ROOT/scripts/install_ea_chart.sh"

echo "=== 4/6 Stop prior stack ==="
bash "$ROOT/scripts/stop_all.sh" 2>/dev/null || true
sleep 2

echo "=== 5/6 Start MT5 terminal first (EA loads from profile) ==="
if ! pgrep -f "terminal64.exe" >/dev/null 2>&1; then
  nohup wine "$MT5_TERMINAL" >/dev/null 2>&1 &
  echo $! > "$PID_DIR/mt5_terminal.pid"
fi
echo "Waiting 12s for MT5 + EA init..."
sleep 12

echo "=== 6/7 Start Python API + trade manager (EA backend) ==="
cd "$ROOT"
nohup "$VENV/bin/uvicorn" src.api.server:app \
  --host 0.0.0.0 --port "$API_PORT" >"$LOG_DIR/api.log" 2>&1 &
echo $! > "$PID_DIR/api.pid"
nohup "$VENV/bin/python" -m src.main >"$LOG_DIR/manager.log" 2>&1 &
echo $! > "$PID_DIR/manager.pid"
sleep 2

echo "=== 7/7 Verify EA TCP connection on port $EA_PORT ==="
connected=0
for i in $(seq 1 90); do
  if grep -q "Connected to MQL5 EA executor" "$LOG_DIR/manager.log" 2>/dev/null; then
    connected=1
    break
  fi
  if grep -q "EA connected from" "$LOG_DIR/manager.log" 2>/dev/null; then
    connected=1
    break
  fi
  sleep 1
done

echo ""
echo "=== Smoke test results ==="
if [[ "$connected" == "1" ]]; then
  echo "EA connected to Python."
else
  echo "WARN: EA did not connect within 90s (attach TradeIdeaExecutor manually if needed)."
  tail -20 "$LOG_DIR/manager.log" 2>/dev/null || true
fi

echo ""
echo "API health:"
curl -sf "http://127.0.0.1:$API_PORT/panel/summary" | head -c 500 || echo "API not responding"
echo ""
echo ""
echo "Panel: http://localhost:$API_PORT/panel"
echo "Logs:  $LOG_DIR/"
