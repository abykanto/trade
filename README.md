# Trade Manager

A modular trading system that ingests trade ideas via a REST API, manages risk and position sizing, and executes orders on MetaTrader 5 through an async bridge.

## Architecture

- **API server** (`src/api/server.py`) — accepts incoming signals, validates them, and persists trade ideas to SQLite.
- **Trade manager** (`src/main.py`) — background worker that monitors ideas, places pending orders, manages open positions, applies chop exits, and handles re-entry.
- **MT5 bridge** (`src/execution/bridge.py`) — async wrapper around [mt5linux](https://pypi.org/project/mt5linux/) (RPyC over Wine) for broker connectivity. **Legacy / reference implementation** — still the default.
- **MQL5 EA executor** (`mql5/Experts/TradeIdeaExecutor.mq5`) — optional native execution path. Set `EXECUTION_BACKEND=ea` to route orders through the EA over TCP. See [mql5/README.md](mql5/README.md).
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

### Local (no Docker)

For day-to-day development without containers:

```bash
bash scripts/start_local.sh      # API + manager (default: mt5linux)
bash scripts/start_local_ea.sh   # same, but EXECUTION_BACKEND=ea
```

`start_local.sh` does **not** start Wine, MT5, or mt5linux — only Python services. Use `start_all.sh` when you need the full mt5linux stack.

Control panel: **http://localhost:8001/panel**

### All-in-one with Wine + mt5linux

```bash
bash scripts/start_all.sh   # MT5 terminal, mt5linux, API, trade manager
bash scripts/stop_all.sh    # stop everything including Wine/MT5
```

### Docker (EA execution from host MT5)

Run the Python brain in a container; keep MetaTrader 5 on the host and attach `TradeIdeaExecutor.mq5`. Point the EA at `127.0.0.1:19520` (published port).

```bash
docker compose up --build
```

MT5 stays outside Docker. Strategy, risk, and state machine remain 100% in Python; the EA is fast broker I/O only.

### Manual

**API server** (receives trade signals):

```bash
uvicorn src.api.server:app --host 0.0.0.0 --port 8001
```

**Trade manager** (executes and monitors trades):

```bash
# Default: Python mt5linux bridge (reference)
python -m src.main

# Optional: MQL5 EA executor (attach TradeIdeaExecutor in MT5 first)
export EXECUTION_BACKEND=ea
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
| `lot_size` | float | no | Fixed lot size; if omitted, dynamic sizing applies on each attempt from remaining risk (capped by `MAX_LOT_SIZE`) |
| `max_retries` | int | no | Re-entry attempts after chop exit (default `25`) |
| `source` | string | no | Unique idempotency key (default `API`) |
| `entry_zone_size` | float | no | Entry zone tolerance as a fraction around entry (default `0.001` = 0.1%). The manager waits for mid price to enter this zone before placing the first pending order. |
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
| `GET /tools/xauusd-signal` | Interactive XAUUSD control panel — chart, place orders, monitor ideas |
| `GET /panel` | Alias for the control panel |
| `GET /ideas?status=active\|terminal\|all` | List trade ideas for the panel |
| `GET /panel/summary` | Active/open/waiting counts and PnL summary |
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
├── ai/           # Text-message signal AI (provider-agnostic + plugins)
│   ├── text_signals/     # Regex parsing, validation, processor
│   └── providers/
│       └── cerebras/     # Cerebras-only — delete to drop this vendor
├── api/          # FastAPI signal ingestion + tool routes
├── core/         # SQLAlchemy models and database setup
├── execution/    # MT5 bridge (legacy), EA bridge, trailing-stop engine
├── market/       # Pending entry, order outcome, candles, price logger
├── risk/         # Session filters, liquidity checks, portfolio risk
└── main.py       # Trade manager entry point
mql5/             # MQL5 EA + includes (optional execution backend)
│   ├── Experts/TradeIdeaExecutor.mq5
│   └── Include/TradeIdea/Protocol.mqh
tools/
└── xauusd_signal.html   # Interactive signal builder UI
scripts/
├── start_all.sh
└── stop_all.sh
tmp/                     # runtime artifacts (gitignored): logs, PIDs, parquet ticks
trade_ideas.db           # SQLite state (gitignored, project root)
tests/
```

## Configuration

| Component | Default | Notes |
|-----------|---------|-------|
| Database | `trade_ideas.db` (project root) | `DATABASE_URL` or `TradeManager(db_url)` |
| MT5 host/port | `localhost:18812` | `MT5_HOST`, `MT5_PORT` in `.env` |
| API port | `8001` | `API_PORT` in `.env` or uvicorn `--port` |
| Price logs | `tmp/data/price_logs/` | `PRICE_LOG_DIR` |
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

The **final hard SL** from the signal (`original_hard_stop`) is kept in the DB for pre-entry invalidation only. While waiting for fill/re-entry, if price hits `original_hard_stop`, the pending order is cancelled and the idea is invalidated. **Lot sizing** uses the distance to `original_hard_stop`; **live broker SL** on each attempt is the tighter chop exit distance from `config.json`.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | — | MT5 account credentials |
| `MT5_HOST` / `MT5_PORT` | `localhost` / `18812` | mt5linux RPyC (legacy backend) |
| `EXECUTION_BACKEND` | `mt5linux` | `mt5linux` or `ea` (MQL5 executor) |
| `EA_SERVER_HOST` / `EA_SERVER_PORT` | `0.0.0.0` / `19520` | TCP listen address when `EXECUTION_BACKEND=ea` |
| `WORKER_LOOP_FAST_SEC` / `WORKER_LOOP_IDLE_SEC` | `0.01` / `0.1` | Symbol worker poll interval when ideas are active vs idle |
| `API_PORT` | `8001` | Trade API port |
| `DAILY_DD_PCT` | `0.03` | Daily drawdown halt (fraction of equity) |
| `MAX_ACCOUNT_RISK_PCT` | `6.0` | Max account risk % |
| `MAX_LOT_SIZE` | — | Cap on dynamic lot sizing |
| `ENABLE_PRIME_SESSION` | `true` | Restrict trading to London/NY overlap; set `false` to trade any UTC hour |
| `PRICE_LOG_INTERVAL_SEC` | `1.0` | Tick logging interval |
| `PRICE_LOG_FLUSH_ROWS` | `30` | Parquet rows buffered before disk flush |
| `PRICE_LOG_DIR` | `tmp/data/price_logs` | Parquet output directory |

Logs and PID files live under `tmp/logs/` and `tmp/run/`. Python log handlers also write `tmp/logs/api_server.log` and `tmp/logs/trade_manager.log`.

## Telegram signal extraction (AI)

Structured Telegram messages can be parsed locally (regex) or via AI fallback:

```bash
export CEREBRAS_API_KEYS="key-one,key-two"
export PYTHONPATH=.
python scripts/extract_signal.py          # demo messages
python scripts/extract_signal.py --stdin  # paste messages, blank line between
```

- **Deterministic first** — no API call when `Pair` / `Type` / `Entry` / `Stop Loss` / TPs are present.
- **AI fallback** — uses [Cerebras Cloud SDK](https://github.com/Cerebras/cerebras-cloud-sdk-python) when regex cannot parse.
- **Multi-model rotation** — rotates across `gemma-4-31b`, `gpt-oss-120b`, and `zai-glm-4.7` with per-model rate tracking (~4 req/min each, 13s spacing) so you stay under dashboard limits.
- **Anti-hallucination** — every AI field must appear in the source message.

| Variable | Default | Purpose |
|----------|---------|---------|
| `CEREBRAS_API_KEY` | — | Single Cerebras API key |
| `CEREBRAS_API_KEYS` | — | Comma-separated keys; rotates with models on rate limits |
| `SIGNAL_AI_PROVIDER` | `cerebras` | `cerebras` or `none` |
| `SIGNALS_URL` | `http://localhost:8001/signals` | Target for generated curl |

To switch providers later: implement `SignalAIProvider` under `src/ai/providers/` and delete `src/ai/providers/cerebras/` if dropping Cerebras.
