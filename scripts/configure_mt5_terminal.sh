#!/usr/bin/env bash
# Patch MT5 common.ini so automated (algo) trading starts enabled.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"

python3 "$ROOT/scripts/configure_mt5_terminal.py" "$@"
