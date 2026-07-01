#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_DIR="$ROOT/tmp/run"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

stop_pid_file() {
  local name="$1"
  local file="$PID_DIR/$name.pid"
  if [[ -f "$file" ]]; then
    local pid
    pid="$(cat "$file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      echo "Stopped $name (pid $pid)"
    fi
    rm -f "$file"
  fi
}

stop_pid_file manager
stop_pid_file api
stop_pid_file mt5linux
stop_pid_file mt5_terminal

pkill -f "src.main" 2>/dev/null || true
pkill -f "src.api.server:app" 2>/dev/null || true
pkill -f "mt5linux" 2>/dev/null || true
pkill -f "terminal64.exe" 2>/dev/null || true

if command -v fuser >/dev/null 2>&1; then
  fuser -k "${MT5_PORT:-18812}/tcp" 2>/dev/null || true
fi

export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
if command -v wineserver >/dev/null 2>&1; then
  wineserver -k 2>/dev/null || true
fi

echo "Trade system stopped (API, manager, MT5 RPyC, Wine/MT5 terminal)."
