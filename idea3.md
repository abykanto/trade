# Worker Architecture

## Design Principle

The system must operate using a dedicated worker per symbol.

Example:

```text
EURUSD Worker

GBPUSD Worker

USDJPY Worker

XAUUSD Worker

XAGUSD Worker

USOIL Worker
```

Each worker is responsible only for its assigned symbol.

---

# Worker Responsibilities

A symbol worker owns:

* Price monitoring
* Trade idea evaluation
* Re-entry detection
* Trailing stop management
* Position state updates
* Risk consumption tracking
* Trade execution requests

Example:

```text
XAUUSD Worker

    ↓

Monitor XAUUSD ticks

    ↓

Evaluate active ideas

    ↓

Update trailing stop

    ↓

Trigger re-entry

    ↓

Send execution request
```

---

# Worker Isolation

Workers must not share state directly.

Bad:

```python
global_positions
global_trade_state
```

Good:

```python
symbol_worker_state["XAUUSD"]
symbol_worker_state["EURUSD"]
symbol_worker_state["GBPUSD"]
```

Each worker owns only its symbol state.

---

# Async Architecture

Workers should be implemented using asyncio.

Example:

```python
asyncio.create_task(symbol_worker("XAUUSD"))

asyncio.create_task(symbol_worker("EURUSD"))

asyncio.create_task(symbol_worker("GBPUSD"))
```

The system should support:

```text
100+ workers
```

without creating operating-system threads.

---

# Worker Lifecycle

```text
START

    ↓

Load Symbol Config

    ↓

Load Active Trade Ideas

    ↓

Subscribe To Price Updates

    ↓

Monitor Market

    ↓

Process Trade Ideas

    ↓

Execute Actions

    ↓

Persist Changes

    ↓

Repeat
```

---

# Symbol State Cache

Each worker maintains an in-memory cache.

Example:

```python
SymbolState:

    symbol

    latest_price

    active_ideas

    open_positions

    last_tick_time
```

Workers should read from memory whenever possible.

Avoid repeated database queries.

Avoid repeated MT5 position scans.

---

# Price Feed Service

A dedicated price feed service should collect prices.

```text
MT5

    ↓

Price Feed Service

    ↓

Shared Memory Cache

    ↓

Symbol Workers
```

Workers should not individually call:

```python
mt5.symbol_info_tick()
```

for every decision.

Instead:

```python
price = price_cache["XAUUSD"]
```

This reduces MT5 API load significantly.

---

# Execution Queue

Workers should never place MT5 orders directly.

Instead:

```text
Worker

    ↓

Execution Queue

    ↓

Order Executor

    ↓

MT5
```

Benefits:

* Centralized execution
* Better logging
* Retry handling
* Duplicate prevention
* Rate limiting

---

# Trade Idea Assignment

Every Trade Idea belongs to exactly one worker.

Example:

```text
Trade Idea #101

Symbol = XAUUSD

Assigned Worker = XAUUSD Worker
```

Only that worker may modify the idea.

---

# Restart Recovery

On startup:

1. Load active trade ideas.
2. Group by symbol.
3. Assign ideas to workers.
4. Rebuild worker state.
5. Resume monitoring.

No trade idea should be orphaned.

---

# Future Scaling

Current:

```text
Single VPS

Single Process

100+ Workers
```

Future:

```text
Worker Process A
    EURUSD
    GBPUSD

Worker Process B
    XAUUSD
    XAGUSD

Worker Process C
    NAS100
    US30
```

The design should allow worker distribution across processes and servers without changing trade idea logic.
