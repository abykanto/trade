#!/usr/bin/env bash
# Allow MQL5 SocketConnect to 127.0.0.1 (required for TradeIdeaExecutor).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/scripts/configure_mt5_terminal.sh" --ea-sockets
