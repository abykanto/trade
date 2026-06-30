# Trade Manager

A modular trading system that ingests trade ideas via a REST API, manages risk and position sizing, and executes orders on MetaTrader 5 through an async bridge.

## Architecture

- **API server** (`src/api/server.py`) — accepts incoming signals, validates them, and persists trade ideas to SQLite.
- **Trade manager** (`src/main.py`) — background worker that monitors ideas, places pending orders, manages open positions, applies chop exits, and handles re-entry.
- **MT5 bridge** (`src/execution/bridge.py`) — async wrapper around [mt5linux](https://pypi.org/project/mt5linux/) (RPyC over Wine) for broker connectivity.
- **Price logger** (`src/market/price_logger.py`) — records XAUUSD ticks to parquet for the signal chart tool.

See [flowchart.md](flowchart.md) for a visual walkthrough of the trade lifecycle.

## Requirements

- Python 3.12+
- MetaTrader 5 terminal with mt5linux server running (default: `localhost:18812`)
- Wine (for MT5 + mt5linux on Linux)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run commands from the project root with `PYTHONPATH=.` so the `src` package resolves correctly:

```bash
export PYTHONPATH=.
```

Copy `.env` with MT5 credentials (`MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`) and any overrides (see [Environment variables](#environment-variables)).

## Running

### All-in-one (recommended)

```bash
bash scripts/start_all.sh   # MT5 terminal, mt5linux, API, trade manager
bash scripts/stop_all.sh    # stop everything including Wine/MT5
```

Default API: **http://localhost:8001**

### Manual

**API server** (receives trade signals):

```bash
uvicorn src.api.server:app --host 0.0.0.0 --port 8001
```

**Trade manager** (executes and monitors trades):

```bash
python -m src.main
```

## Submitting signals

### `POST /signals`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | string | yes | e.g. `XAUUSD`, `EURUSD` |
| `direction` | string | yes | `BUY` or `SELL` |
| `entry_price` | float | yes | Pending order price |
| `hard_stop` | float | yes | Final hard SL (pre-fill invalidation + risk reference) |
| `take_profit` | float | yes | Take-profit level |
| `max_idea_risk` | float | yes | Max USD risk budget for the idea (must be > 0) |
| `lot_size` | float | no | Fixed lot size; if omitted, dynamic sizing applies (capped by `MAX_LOT_SIZE`) |
| `max_retries` | int | no | Re-entry attempts after chop exit (default `25`) |
| `source` | string | no | Unique idempotency key (default `API`) |
| `entry_zone_size` | float | no | Entry zone tolerance (default `0.001`) |
| `expires_in_days` | int | no | Idea expiry (default `5`) |
| `external_reference` | string | no | Optional external reference |

**Level geometry**

- **BUY**: `hard_stop` < `entry_price` < `take_profit`
- **SELL**: `take_profit` < `entry_price` < `hard_stop`

**EURUSD example**

```bash
curl -X POST http://localhost:8001/signals \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "EURUSD",
    "direction": "BUY",
    "entry_price": 1.0850,
    "hard_stop": 1.0800,
    "take_profit": 1.0950,
    "max_idea_risk": 10.0,
    "source": "eurusd_manual_1"
  }'
```

**XAUUSD example**

```bash
curl -X POST http://localhost:8001/signals \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "XAUUSD",
    "direction": "SELL",
    "entry_price": 4243.00,
    "hard_stop": 4248.00,
    "take_profit": 4223.00,
    "max_idea_risk": 2.5,
    "lot_size": 0.03,
    "source": "xau_sell_2026-06-18"
  }'
```

Use a **unique `source`** per new idea; duplicates are rejected.

### Other API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /ideas/{idea_id}` | Fetch a trade idea by id |
| `GET /tools/xauusd-signal` | Interactive XAUUSD signal builder (HTML) |
| `GET /tools/xauusd-candles?minutes=180` | 1-minute OHLC from parquet ticks (for the chart tool) |

## XAUUSD signal builder

Open **http://localhost:8001/tools/xauusd-signal** in a browser (trade manager must be running to collect live ticks).

Features:

- **1m candlestick chart** from parquet tick logs (auto-refreshes every 10s)
- **Draggable entry / SL / TP** lines; move handle to shift all three together
- **Live price** marker (faint dotted line)
- **R:R presets**: `1:3`, `1:4`, `1:5`, `1:6`, `2:7`, `1.5:4.5`, `3:10`, or custom risk:reward
- **Consolidation recommendation** — analyses the last 30 × 1m bars, detects the tightest 70% price cluster, and suggests direction, levels, and R:R
- **Reset levels** — restores the snapshot from page load
- **Copy curl** — generates the `POST /signals` command from current chart levels

## Tests

```bash
pytest
```

Tests use an in-memory SQLite database and do not require a live MT5 connection.

## Project structure

```
src/
├── api/          # FastAPI signal ingestion + tool routes
├── core/         # SQLAlchemy models and database setup
├── execution/    # MT5 bridge and trailing-stop engine
├── market/       # Pending entry, order outcome, candles, price logger
├── risk/         # Session filters, liquidity checks, portfolio risk
└── main.py       # Trade manager entry point
tools/
└── xauusd_signal.html   # Interactive signal builder UI
scripts/
├── start_all.sh
└── stop_all.sh
data/price_logs/         # xauusd_ticks_YYYY-MM-DD.parquet
tests/
```

## Configuration

| Component | Default | Notes |
|-----------|---------|-------|
| Database | `sqlite:///trade_ideas.db` | Set via `TradeManager(db_url)` |
| MT5 host/port | `localhost:18812` | `MT5_HOST`, `MT5_PORT` in `.env` |
| API port | `8001` | `API_PORT` in `.env` or uvicorn `--port` |
| Price logs | `data/price_logs/` | `PRICE_LOG_DIR` |
| Chop exit | `config.json` | See below |

### Chop exit distance (`config.json`)

Whipsaw chop exit: close open trades when price moves against entry by this many **price points**.

```json
{
  "chop_exit_distance": 1.0,
  "symbol_chop_exit_distance": {
    "XAUUSD": 1.0,
    "EURUSD": 0.0001
  }
}
```

- **BUY** @ 4352 with distance `1.0` → working stop at **4351**
- **SELL** @ 4352 with distance `1.0` → working stop at **4353**
- Override globally: `CHOP_EXIT_DISTANCE=2.0`
- Override per symbol: `CHOP_EXIT_DISTANCE_XAUUSD=2.0`

The **final hard SL** from the signal (`original_hard_stop`) is kept in the DB for pre-entry invalidation only. While waiting for fill/re-entry, if price hits `original_hard_stop`, the pending order is cancelled and the idea is invalidated.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | — | MT5 account credentials |
| `MT5_HOST` / `MT5_PORT` | `localhost` / `18812` | mt5linux RPyC |
| `API_PORT` | `8001` | Trade API port |
| `DAILY_DD_PCT` | `0.03` | Daily drawdown halt (fraction of equity) |
| `MAX_ACCOUNT_RISK_PCT` | `6.0` | Max account risk % |
| `MAX_LOT_SIZE` | — | Cap on dynamic lot sizing |
| `ENABLE_PRIME_SESSION` | `true` | Restrict trading to London/NY overlap; set `false` to trade any UTC hour |
| `PRICE_LOG_INTERVAL_SEC` | `1.0` | Tick logging interval |
| `PRICE_LOG_DIR` | `data/price_logs` | Parquet output directory |

Logs are written to `logs/api.log`, `logs/manager.log`, `trade_manager.log`, and `api_server.log`.
