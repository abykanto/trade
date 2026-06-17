# Trade Signal Ingestion Layer

## Goal

The trading system must support multiple sources of incoming trade ideas.

Supported sources:

1. Manual User Input
2. Telegram Bot
3. REST API
4. Internal Python Service
5. Future Web Dashboard

All sources must ultimately create a Trade Idea record.

---

# Signal Source Architecture

```text
Telegram Bot
       |
REST API
       |
Manual Input
       |
External Python Service
       |
       v

Signal Gateway

       v

Trade Idea Manager

       v

Database
```

No source should communicate directly with MT5.

All signals must pass through the Trade Idea Manager.

---

# Signal Payload Format

Every signal should be normalized into:

```json
{
    "symbol": "XAUUSD",
    "direction": "BUY",
    "entry": 1000.50,
    "stop_loss": 996.50,
    "take_profit": 1012.50,
    "source": "telegram",
    "external_reference": "msg_123456"
}
```

---

# Database Design

## trade_ideas

Primary table.

One row per trading idea.

```sql
CREATE TABLE trade_ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    symbol TEXT NOT NULL,

    direction TEXT NOT NULL,

    source TEXT NOT NULL,

    external_reference TEXT,

    original_entry REAL NOT NULL,

    hard_stop REAL NOT NULL,

    take_profit REAL NOT NULL,

    entry_zone_low REAL NOT NULL,

    entry_zone_high REAL NOT NULL,

    max_retries INTEGER NOT NULL,

    retries_used INTEGER DEFAULT 0,

    max_idea_risk REAL NOT NULL,

    consumed_risk REAL DEFAULT 0,

    realized_pnl REAL DEFAULT 0,

    state TEXT NOT NULL,

    created_at DATETIME NOT NULL,

    expires_at DATETIME,

    updated_at DATETIME NOT NULL
);
```

---

## trade_attempts

Stores every MT5 entry attempt.

One Trade Idea can have many attempts.

```sql
CREATE TABLE trade_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    trade_idea_id INTEGER NOT NULL,

    mt5_ticket TEXT,

    attempt_number INTEGER NOT NULL,

    entry_price REAL NOT NULL,

    exit_price REAL,

    quantity REAL,

    pnl REAL,

    exit_reason TEXT,

    opened_at DATETIME NOT NULL,

    closed_at DATETIME,

    FOREIGN KEY(trade_idea_id)
    REFERENCES trade_ideas(id)
);
```

---

## trade_events

Audit trail.

Never delete records.

```sql
CREATE TABLE trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    trade_idea_id INTEGER NOT NULL,

    event_type TEXT NOT NULL,

    event_data TEXT,

    created_at DATETIME NOT NULL,

    FOREIGN KEY(trade_idea_id)
    REFERENCES trade_ideas(id)
);
```

Examples:

```text
IDEA_CREATED

ENTRY_PLACED

EARLY_EXIT

REENTRY

TRAILING_MOVED

RISK_EXHAUSTED

TP_HIT

IDEA_EXPIRED
```

---

## open_positions

Current MT5 positions.

Used for crash recovery.

```sql
CREATE TABLE open_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    trade_idea_id INTEGER NOT NULL,

    mt5_ticket TEXT NOT NULL,

    symbol TEXT NOT NULL,

    direction TEXT NOT NULL,

    entry_price REAL NOT NULL,

    current_stop REAL,

    current_tp REAL,

    trailing_stop REAL,

    opened_at DATETIME NOT NULL,

    FOREIGN KEY(trade_idea_id)
    REFERENCES trade_ideas(id)
);
```

---

# Telegram Integration

Telegram messages should create Trade Ideas.

Example:

```text
BUY XAUUSD

ENTRY: 1000.50
SL: 996.50
TP: 1012.50
```

Telegram service parses message.

Creates a normalized signal.

Inserts Trade Idea.

Returns generated Trade Idea ID.

---

# External Service Integration

A separate Python service should be able to submit signals.

Preferred mechanism:

```http
POST /trade-ideas
```

Payload:

```json
{
    "symbol": "EURUSD",
    "direction": "BUY",
    "entry": 1.1450,
    "sl": 1.1410,
    "tp": 1.1570
}
```

The trading engine validates and creates the Trade Idea.

---

# Recovery Requirements

On startup:

1. Load active Trade Ideas.
2. Load open positions.
3. Reconstruct in-memory state.
4. Resume monitoring.
5. Resume trailing logic.
6. Resume retry tracking.

No active idea should be lost after restart.

---

# Analytics Requirements

The system must support:

* Total Ideas
* Active Ideas
* Expired Ideas
* TP Hit Ideas
* Risk Exhausted Ideas
* Average Attempts Per Idea
* Average Consumed Risk
* Average Idea PnL
* Largest Winner
* Largest Loser
* Win Rate By Symbol
* Profit By Symbol
* Retry Distribution
* Whipsaw Recovery Efficiency

All analytics should be computed from database records.
