# MQL5 EA Executor

Native MetaTrader 5 Expert Advisor that executes broker operations for the Python
Trade Manager. The legacy Python `MT5Bridge` (`mt5linux`) remains in the codebase
for reference; use this EA when you want faster, in-terminal execution.

## Architecture

```text
Python TradeManager  ←——TCP JSON lines——→  TradeIdeaExecutor.mq5 (EA)
     (EXECUTION_BACKEND=ea)                        MT5 terminal
```

- Python listens on `EA_SERVER_HOST:EA_SERVER_PORT` (default `0.0.0.0:19520`)
- EA connects **to** Python on chart attach (client mode)
- Python sends commands (`PLACE_PENDING`, `MODIFY_POSITION`, `GET_ORDER_HISTORY`, `CLOSE_POSITION`, …)
- Pending-order outcome is resolved in **Python** via `GET_ORDER_HISTORY` + live book snapshots (not a legacy `RESOLVE_PENDING` command)
- EA verifies pending orders rest on-book after placement (bad-fill emergency close)
- EA pushes trade events (`POSITION_OPENED`, `POSITION_CLOSED`, …) — Python wakes workers immediately
- **All strategy logic stays in Python**; `resolve_pending_order_outcome` runs in Python using EA snapshots

## Install into MT5

### Automated (Linux + Wine)

From the repo root:

```bash
bash scripts/sync_compile_ea.sh      # copy sources + compile → TradeIdeaExecutor.ex5
bash scripts/enable_ea_sockets.sh    # allow SocketConnect to 127.0.0.1 in common.ini
bash scripts/install_ea_chart.sh     # optional: auto-attach on Default/XAUUSD chart
bash scripts/smoke_run_ea.sh         # compile + start MT5 + Python (EA backend)
```

Compile requires `xvfb-run` and Wine MetaEditor. The compile step must run from the MT5
install directory (handled by the script).

**MT5 terminal setting:** Tools → Options → Expert Advisors → enable *Allow WebRequest for
listed URL* and add `127.0.0.1` (no port). `enable_ea_sockets.sh` patches `common.ini`
for you (UTF-16-LE).

> **Wine note:** On some Linux/Wine setups MQL5 `SocketConnect` still returns error
> `4014` even with the allowlist. If that happens, use `EXECUTION_BACKEND=mt5linux`
> (`bash scripts/start_all.sh`) on the same machine, or run Python in Docker and attach
> the EA from native Windows MT5.

### Manual (MetaEditor)

1. Open MetaEditor (from MT5: **Tools → MetaQuotes Language Editor**)
2. Copy this repo folder into your MT5 data directory:
   - `mql5/Experts/TradeIdeaExecutor.mq5` → `MQL5/Experts/`
   - `mql5/Include/TradeIdea/` → `MQL5/Include/TradeIdea/`
3. Compile `TradeIdeaExecutor.mq5` (F7)
4. In MT5:
   - Open any chart (e.g. XAUUSD)
   - **Navigator → Expert Advisors → TradeIdeaExecutor**
   - Drag onto chart
   - Set inputs:
     - `InpPythonHost` = host running Python (`127.0.0.1` on same machine)
     - `InpPythonPort` = `19520` (or `EA_SERVER_PORT`)
     - `InpMagicNumber` = `234000` (must match Python)
   - Enable **Algo Trading** (toolbar button must be green)

## Run Python with EA backend

**Local (no Docker):**

```bash
bash scripts/start_local_ea.sh
```

**Docker** (MT5 on host, EA connects to published port `19520`):

```bash
docker compose up --build
```

**Manual:**

```bash
export PYTHONPATH=.
export EXECUTION_BACKEND=ea
export EA_SERVER_PORT=19520
python -m src.main
```

Start Python **before** attaching the EA, or restart the EA after Python is up.

## Protocol

Newline-delimited JSON. See `src/execution/protocol.py` for command constants.

**Python → EA (examples)**

```json
{"type":"PING","id":"abc123"}
{"type":"PLACE_PENDING","id":"def456","symbol":"XAUUSD","direction":"BUY","volume":0.05,"entry":4360,"sl":4359,"tp":4370,"bid":4355,"ask":4355.2,"order_type":4,"order_kind":"BUY_STOP","tick_size":0.01,"magic":234000}
```

**EA → Python**

```json
{"type":"CONNECTED","magic":234000,"terminal":"MetaTrader 5","account":12345678}
{"type":"OK","id":"def456","retcode":10009,"order":55501}
{"type":"TRADE_EVENT","event":"POSITION_OPENED","ticket":9001,"symbol":"XAUUSD","price":4360,"profit":0,"magic":234000}
```

## Docker / Wine notes

When running MT5 under Wine in Docker:

- Map port `19520` from the container
- Set `InpPythonHost` to the Docker service name or `host.docker.internal`
- MT5 still needs a virtual display (`Xvfb`) and Algo Trading enabled inside the container
- Keep the Python reference bridge (`EXECUTION_BACKEND=mt5linux`) for local dev without the EA

## Files

| File | Purpose |
|------|---------|
| `Experts/TradeIdeaExecutor.mq5` | Main EA — attach to chart |
| `Include/TradeIdea/Protocol.mqh` | JSON helpers shared with EA |

Python counterparts:

| File | Purpose |
|------|---------|
| `src/execution/ea_bridge.py` | Drop-in bridge using EA |
| `src/execution/ea_server.py` | TCP server for EA connection |
| `src/execution/protocol.py` | Message encoding |
| `src/execution/factory.py` | `EXECUTION_BACKEND` switch |
| `src/execution/bridge.py` | Legacy mt5linux bridge (unchanged) |
