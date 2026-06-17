# Trade Idea Management System Specification

## Goal

Build an automated MT5 + Python trading system that manages **trade ideas** rather than individual trades.

The system should:

* Support multiple symbols simultaneously.
* Support long-running trades (1-5 days).
* Allow controlled re-entry after early exits.
* Track cumulative losses across multiple attempts.
* Stop retrying once the trade idea exceeds its allowed risk budget.
* Use trailing protection to recover previous whipsaw losses.
* Be fully configurable per instrument.

---

# Core Philosophy

Traditional trading:

```text
Entry
  ↓
Stop Loss OR Take Profit
```

This system:

```text
Trade Idea
    ↓
Attempt #1
Attempt #2
Attempt #3
...
Take Profit
OR
Risk Budget Exhausted
```

The unit of risk is the  **trade idea** , not the individual trade.

---

# Supported Instruments

Examples:

* EURUSD
* GBPUSD
* USDJPY
* AUDUSD
* USDCAD
* USDCHF
* NZDUSD
* XAUUSD
* XAGUSD
* USOIL
* UKOIL
* NAS100
* US30
* GER40

The system must allow arbitrary symbols through configuration.

---

# Instrument Configuration

---

Each symbol should have its own configuration.

Example:

```yaml
symbols:

  XAUUSD:
    entry_zone_size: 1.0
    soft_exit_distance: 1.5
    hard_stop_distance: 4.0
    target_distance: 12.0
    max_retries: 5
    max_idea_risk: 4.0
    lot_size: 0.01

  EURUSD:
    entry_zone_size: 0.0005
    soft_exit_distance: 0.0010
    hard_stop_distance: 0.0030
    target_distance: 0.0090
    max_retries: 5
    max_idea_risk: 0.0030
    lot_size: 0.01
```

---

# Trade Idea Lifecycle

## States

```text
WAITING_FOR_SETUP

ENTRY_ZONE_REACHED

TRADE_OPEN

EARLY_EXIT

WAITING_FOR_REENTRY

TP_REACHED

RISK_EXHAUSTED

IDEA_EXPIRED
```

---

# Trade Idea Object

Each idea should maintain:

```python
TradeIdea:
    id
    symbol

    direction

    original_entry

    entry_zone_low
    entry_zone_high

    hard_stop

    take_profit

    max_idea_risk

    consumed_risk

    retries_used

    max_retries

    created_at

    expiry_time

    state
```

---

# Entry Logic

A setup creates a new Trade Idea.

Example:

```text
BUY

Entry = 1000.50

Entry Zone:
1000.00 - 1001.00

Hard Stop:
996.50

Take Profit:
1012.50
```

The system enters the first trade.

---

# Early Exit Logic

Purpose:

Avoid sitting through unnecessary drawdowns.

Example:

```text
Entry:
1000.50

Price:
999.00

Close Trade

Loss:
1.50
```

The loss should be recorded against the Trade Idea.

```python
idea.consumed_risk += 1.50
```

The idea remains ACTIVE.

---

# Re-Entry Logic

After early exit:

Wait for price to return into the configured entry zone.

Example:

```text
Entry Zone:
1000.00 - 1001.00

Price:
1000.75

Allowed Re-entry
```

Re-entry should only occur when:

```text
Consumed Risk < Max Idea Risk

AND

Retries Used < Max Retries

AND

Idea Not Expired
```

---

# Retry Budget

Example:

```text
Max Retries = 5
```

After:

```text
Retry #5
```

No further entries are allowed.

---

# Risk Budget

Example:

```text
Max Idea Risk = 4.00
```

Sequence:

```text
Loss #1 = 1.50

Loss #2 = 0.80

Loss #3 = 0.70
```

Total:

```text
3.00
```

Still allowed.

If:

```text
Consumed Risk >= 4.00
```

State becomes:

```text
RISK_EXHAUSTED
```

No more entries.

---

# Take Profit

Example:

```text
Entry = 1000.50

TP = 1012.50
```

The system should attempt to capture the larger move.

---

# Trailing Recovery Logic

This is a key requirement.

The strategy may incur multiple small losses before catching the trend.

Example:

```text
Loss #1 = 1.50

Loss #2 = 0.80

Loss #3 = 0.70

Total Whipsaw Loss = 3.00
```

When the trade finally moves in profit:

```text
Current Profit = 4.00
```

The system should immediately secure at least:

```text
3.00
```

because previous whipsaw losses have already consumed risk.

---

# Dynamic Trailing Stop

Example:

```text
Accumulated Whipsaw Loss = 3.00

Current Trade Profit = 5.00
```

Move stop such that:

```text
Minimum Secured Profit >= 3.00
```

This guarantees recovery of previous losses.

---

# Progressive Trailing

Example:

```text
Profit = 5
Secure = 3

Profit = 8
Secure = 5

Profit = 12
Secure = 9
```

Trailing should tighten as the trade approaches the target.

---

# Idea Expiry

Trade ideas should not live forever.

Example:

```text
Max Lifetime = 5 Days
```

If:

```text
Now > Created Time + 5 Days
```

Then:

```text
IDEA_EXPIRED
```

No more entries.

---

# Multi-Symbol Support

System should support:

```text
100+ active trade ideas
```

simultaneously.

Each symbol maintains independent state.

Example:

```text
XAUUSD
    Idea #1

EURUSD
    Idea #2

GBPUSD
    Idea #3
```

No shared risk tracking between symbols.

---

# System Architecture

```text
MT5

    ↓

Price Feed Layer

    ↓

Strategy Engine

    ↓

Trade Idea Manager

    ↓

Risk Manager

    ↓

Order Executor

    ↓

MT5
```

---

# Persistence

Persist:

* Trade Ideas
* Open Positions
* Consumed Risk
* Retry Counts
* Realized PnL
* State Transitions

Storage:

* SQLite initially
* PostgreSQL later

---

# Logging Requirements

Log every event:

```text
IDEA_CREATED

ENTRY

EARLY_EXIT

REENTRY

TRAILING_UPDATE

TP_HIT

RISK_EXHAUSTED

IDEA_EXPIRED
```

Each log entry should contain:

```text
Timestamp
Symbol
Idea ID
Price
PnL
State
```

---

# Non-Functional Requirements

* Python 3.12+
* MT5 Integration
* Async Architecture (asyncio)
* Config Driven
* Multi-Symbol Support
* Restart Safe
* State Recovery After Crash
* Detailed Audit Logs
* Paper Trading Mode
* Live Trading Mode
* Unit Testable Components

---

# Success Criteria

The system successfully:

1. Manages trade ideas instead of single trades.
2. Allows controlled re-entry after early exits.
3. Tracks cumulative whipsaw losses.
4. Enforces retry limits.
5. Enforces total idea risk limits.
6. Recovers previous whipsaw losses through trailing protection.
7. Captures large multi-day moves.
8. Supports multiple instruments simultaneously.
9. Survives restarts without losing state.
10. Operates autonomously on a VPS 24/7.

```

```
