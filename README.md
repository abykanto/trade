# Trade Manager

A modular trading system that ingests trade ideas via a REST API, manages risk and position sizing, and executes orders on MetaTrader 5 through an async bridge.

## Architecture

- **API server** (`src/api/server.py`) — accepts incoming signals, validates them, and persists trade ideas to SQLite.
- **Trade manager** (`src/main.py`) — background worker that monitors ideas, places pending orders, manages open positions, and applies trailing stops.
- **MT5 bridge** (`src/execution/bridge.py`) — async wrapper around [mt5linux](https://pypi.org/project/mt5linux/) (RPyC over Wine) for broker connectivity.

See [flowchart.md](flowchart.md) for a visual walkthrough of the trade lifecycle.

## Requirements

- Python 3.12+
- MetaTrader 5 terminal with mt5linux server running (default: `localhost:18812`)

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

## Running

**Start the API server** (receives trade signals):

```bash
uvicorn src.api.server:app --host 0.0.0.0 --port 8000
```

**Start the trade manager** (executes and monitors trades):

```bash
python -m src.main
```

Submit a signal:

```bash
curl -X POST http://localhost:8000/signals \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "EURUSD",
    "direction": "BUY",
    "entry_price": 1.0850,
    "hard_stop": 1.0800,
    "take_profit": 1.0950,
    "max_idea_risk": 10.0
  }'
```

## Tests

```bash
pytest
```

Tests use an in-memory SQLite database and do not require a live MT5 connection.

## Project structure

```
src/
├── api/          # FastAPI signal ingestion
├── core/         # SQLAlchemy models and database setup
├── execution/    # MT5 bridge and trailing-stop engine
├── risk/         # Session filters, liquidity checks, portfolio risk
└── main.py       # Trade manager entry point
tests/
```

## Configuration

| Component      | Default                    | Notes                          |
|----------------|----------------------------|--------------------------------|
| Database       | `sqlite:///trade_ideas.db` | Set via `TradeManager(db_url)` |
| MT5 host/port  | `localhost:18812`          | Set via `MT5Bridge(host, port)` |
| API port       | `8000`                     | Set via uvicorn `--port`       |

Logs are written to `trade_manager.log` and `api_server.log`.
