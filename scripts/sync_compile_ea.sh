#!/usr/bin/env bash
# Copy TradeIdea EA sources into Wine MT5 and compile with MetaEditor.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
MT5_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"
MQL5="$MT5_DIR/MQL5"
EA_MQ5="$MQL5/Experts/TradeIdeaExecutor.mq5"
EA_LOG="$MQL5/Experts/TradeIdeaExecutor.log"
EA_EX5="$MQL5/Experts/TradeIdeaExecutor.ex5"
METAEDITOR="$MT5_DIR/MetaEditor64.exe"

mkdir -p "$MQL5/Experts" "$MQL5/Include/TradeIdea"
cp "$ROOT/mql5/Experts/TradeIdeaExecutor.mq5" "$MQL5/Experts/"
cp "$ROOT/mql5/Include/TradeIdea/Protocol.mqh" "$MQL5/Include/TradeIdea/"

export WINEDEBUG="${WINEDEBUG:--all}"
export DISPLAY="${DISPLAY:-:0}"

echo "Compiling TradeIdeaExecutor.mq5 via MetaEditor (from MT5 install dir)..."
rm -f "$EA_EX5" "$EA_LOG"

if command -v xvfb-run >/dev/null 2>&1; then
  RUNNER=(xvfb-run -a)
else
  RUNNER=()
fi

(
  cd "$MT5_DIR"
  "${RUNNER[@]}" wine ./MetaEditor64.exe \
    /compile:MQL5\\Experts\\TradeIdeaExecutor.mq5 \
    /log:MQL5\\Experts\\TradeIdeaExecutor.log \
    || true
)

if [[ ! -f "$EA_EX5" ]]; then
  echo "ERROR: TradeIdeaExecutor.ex5 was not produced."
  if [[ -f "$EA_LOG" ]]; then
    echo "--- compile log (tail) ---"
    python3 - "$EA_LOG" <<'PY'
import sys
data = open(sys.argv[1], "rb").read()
text = data.decode("utf-16-le", errors="replace")
print(text[-3000:])
PY
  fi
  exit 1
fi

echo "OK: $(ls -la "$EA_EX5")"
if [[ -f "$EA_LOG" ]]; then
  echo "--- compile result ---"
  python3 - "$EA_LOG" <<'PY'
import sys
data = open(sys.argv[1], "rb").read()
for line in data.decode("utf-16-le", errors="replace").splitlines():
    if "Result:" in line or "error" in line.lower():
        print(line)
PY
fi
