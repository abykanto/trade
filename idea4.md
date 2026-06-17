# FINAL ADDENDUM — Production Readiness, Risk Controls & Edge Cases

This document supplements all previous specifications.

The purpose is to define all missing behavior, edge cases, portfolio controls, and operational safeguards.

---

# Trade Idea Invalidation

A trade idea can end in four ways:

```text
TP_REACHED

RISK_EXHAUSTED

IDEA_EXPIRED

IDEA_INVALIDATED
```

---

## Idea Invalidated

The strategy must support custom invalidation logic.

Examples:

```text
Breakout failed

Trend reversed

Structure broken

External signal cancelled
```

When invalidated:

```text
Close all active positions

Cancel future re-entries

Mark idea as INVALIDATED
```

No further retries allowed.

---

# Symbol Ownership Rules

Default Rule:

```text
One Active Trade Idea Per Symbol
```

Example:

```text
XAUUSD

Idea #101 ACTIVE

Idea #102 REJECTED
```

Reason:

Avoid overlapping ideas competing for the same symbol.

Optional future setting:

```yaml
allow_multiple_ideas_per_symbol: true
```

---

# Position Sizing Engine

Never hardcode lot size.

Lot size should be calculated.

Inputs:

```text
Account Balance

Risk Per Idea

Stop Distance

Instrument Contract Size
```

Example:

```text
Risk = $10

Stop = 5 Points

Position Size =
Risk / Stop Distance
```

Configuration:

```yaml
risk_per_idea_percent: 1
```

or

```yaml
risk_per_idea_fixed_amount: 10
```

---

# Portfolio Risk Manager

Trade-level risk is insufficient.

Need account-level protection.

---

## Daily Loss Limit

Example:

```yaml
daily_loss_limit: 50
```

If exceeded:

```text
No new entries

Existing trades remain managed
```

---

## Max Concurrent Ideas

Example:

```yaml
max_active_ideas: 10
```

---

## Max Account Risk

Example:

```yaml
max_account_risk_percent: 5
```

No new ideas allowed beyond threshold.

---

# Correlation Protection

Prevent overexposure.

Example:

```text
EURUSD BUY

GBPUSD BUY

AUDUSD BUY

NZDUSD BUY
```

These are highly correlated.

Risk manager should support:

```yaml
max_correlated_positions: 2
```

Optional future enhancement.

---

# Re-entry Confirmation Rules

Returning to entry zone alone is insufficient.

Minimum requirements:

```text
Price Returns To Zone

AND

Idea Still Valid
```

Optional confirmations:

```text
Momentum Confirmation

Volume Confirmation

Breakout Confirmation

Trend Confirmation
```

Configurable per strategy.

---

# Entry Zone Logic

Example:

```text
Original Entry = 1000.50

Zone Size = 1.00
```

Generated Zone:

```text
1000.00 -> 1001.00
```

All re-entry checks use the zone.

Never exact entry price.

---

# Gap Handling

---

## Gap Beyond TP

Example:

```text
TP = 1020

Market Opens = 1028
```

Close immediately.

Record actual realized profit.

---

## Gap Beyond Stop

Example:

```text
SL = 996

Market Opens = 990
```

Exit immediately.

Record actual realized loss.

Do not assume stop execution price.

---

# Weekend Rules

Configurable.

Example:

```yaml
allow_weekend_holding: true
```

If false:

```text
Close positions before market close
```

---

# Duplicate Signal Protection

Each incoming signal should generate:

```text
Signal Fingerprint
```

Example:

```text
symbol

direction

entry

tp

sl
```

Duplicates within configurable window:

```yaml
duplicate_window_minutes: 10
```

should be rejected.

---

# Execution State Machine

Trade execution must have states.

```text
PENDING

SUBMITTED

ACCEPTED

FILLED

PARTIAL_FILL

REJECTED

CANCELLED
```

Never assume order success.

---

# Trailing Recovery Formula

Core Requirement.

---

## Variables

```text
Consumed Risk

Current Floating Profit

Target Profit
```

---

## Rule

When:

```text
Current Profit >
Consumed Risk
```

Trailing stop should secure:

```text
Consumed Risk
```

minimum.

---

Example

```text
Consumed Risk = 3

Current Profit = 5
```

Secure:

```text
+3
```

minimum.

---

## Progressive Trailing

Example:

```text
Profit = 6

Secure = 3
```

```text
Profit = 10

Secure = 6
```

```text
Profit = 15

Secure = 10
```

Trail becomes tighter as TP approaches.

---

# MT5 Connectivity Recovery

Worker states:

```text
ACTIVE

DEGRADED

DISCONNECTED
```

---

## DEGRADED

Example:

```text
No tick received
```

for configured interval.

Worker pauses entries.

---

## DISCONNECTED

Example:

```text
MT5 offline
```

Actions:

```text
Stop entries

Continue monitoring connection

Auto-reconnect
```

---

# Database Concurrency

Every table should contain:

```sql
version INTEGER DEFAULT 1
```

and

```sql
updated_at DATETIME
```

Updates must verify latest version.

Prevents worker conflicts.

---

# Backtesting Engine

Required before live deployment.

Architecture:

```text
Strategy Logic

    ↓

Backtest Adapter

OR

Live Adapter
```

Same strategy code.

Different data source.

---

# Backtesting Metrics

Must compute:

```text
Total Ideas

Winning Ideas

Losing Ideas

Average Idea PnL

Average Attempts Per Idea

Average Risk Consumed

Retry Usage

Trailing Efficiency

Idea Lifetime

Profit Factor

Max Drawdown
```

---

# Dashboard Metrics

Real-time metrics:

```text
Active Ideas

Open Positions

Daily PnL

Consumed Risk

Retry Usage

Trailing Status

MT5 Status

Worker Status
```

---

# Signal Source Priority

Priority order:

```text
Manual User Input

Telegram

REST API

Internal Service
```

If duplicate ideas arrive:

Higher priority source wins.

---

# Crash Recovery

On startup:

```text
Load Active Ideas

Load Open Positions

Load Workers

Load Pending Orders

Load Trailing State

Resume Processing
```

No manual intervention.

---

# Audit Trail Requirements

Nothing is deleted.

All changes recorded.

Examples:

```text
IDEA_CREATED

IDEA_UPDATED

ENTRY

EXIT

REENTRY

TRAILING_UPDATE

INVALIDATED

RISK_EXHAUSTED

TP_HIT
```

System must support complete reconstruction of any trade idea.

---

# Future Expansion Support

Design should allow:

```text
Multiple Brokers

Multiple MT5 Terminals

Multiple VPS Nodes

Portfolio Management

Machine Learning Filters

Web Dashboard

Mobile Notifications
```

without changing core Trade Idea logic.

---

# Final Principle

The system does NOT manage trades.

The system manages:

```text
Trade Ideas
    →
Attempts
    →
Risk Budget
    →
Profit Capture
```

Every trade, retry, stop movement, trailing action, and profit target exists only to serve the lifecycle of a Trade Idea.
