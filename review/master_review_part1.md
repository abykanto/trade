# Master Review — Part 1 of 3

# Executive Summary

**Verdict: Major Redesign Required**

This system attempts something architecturally ambitious—managing trade *ideas* with retry budgets, whipsaw recovery trailing, and portfolio risk controls. The concept is sound and potentially superior to naive single-entry stop-loss systems.

However, the implementation is a **skeleton prototype** that covers approximately 25-30% of the specification. Critical subsystems are entirely missing, implemented incorrectly, or dangerously simplified. Deploying this with real capital in its current state would result in **certain financial loss**.

**Critical Failures Identified:**

| Category | Severity | Count |
|---|---|---|
| Will-lose-money bugs | 🔴 CRITICAL | 7 |
| Missing safety systems | 🔴 CRITICAL | 12 |
| Incorrect implementations | 🟠 HIGH | 9 |
| Design flaws | 🟠 HIGH | 8 |
| Missing features | 🟡 MEDIUM | 15+ |

**Top 5 most dangerous issues:**

1. **No actual position closing** — The system detects stop hits and TP hits but never sends a close order to MT5. It updates the database state and assumes the position is closed. Real money vanishes.
2. **Loss calculation uses wrong price** — `abs(idea.original_entry - current_price)` always uses the original entry, not the actual entry price of the current attempt. After re-entries at different prices, consumed_risk is fiction.
3. **No retry budget check before re-entry** — The worker checks if price is in the zone but never validates `retries_used < max_retries` or `consumed_risk < max_idea_risk` before placing a new order.
4. **No TradeAttempt records created** — Entry/exit never creates TradeAttempt rows. The entire audit trail, PnL tracking, and recovery mechanism is broken.
5. **Order result completely ignored** — `place_order()` return value is discarded. The system assumes every order succeeds.

---

# Architecture Review

## What it attempts
A layered async system: Price Feed → Symbol Workers → Execution Queue → MT5 Bridge, with portfolio risk gating at the API ingestion layer.

## Correctness Assessment: 4/10

### Strengths
- Worker-per-symbol isolation is architecturally correct
- Execution queue decouples decision from execution
- Price cache prevents N×symbol MT5 API calls
- SQLAlchemy ORM with version column is a reasonable foundation

### Critical Architectural Failures

#### 1. Two Separate Database Instances
The API server (`server.py`) calls `init_db()` independently, creating its own engine and session factory. The `TradeManager` in `main.py` does the same. **These are two separate SQLite connections.** SQLite with concurrent writers from different processes will cause `database is locked` errors under load. Even if run in-process, there's no shared session management.

#### 2. No Worker Dynamic Registration
Workers are only created at startup for symbols that already have active ideas. If a new idea arrives via the API for a symbol that had no prior ideas, **no worker exists for it**. The idea sits in `WAITING_FOR_SETUP` forever.

#### 3. Blocking MT5 Calls in Async Context
`MT5Bridge` methods are synchronous. `price_feed_loop()` calls `self.bridge.get_symbol_info(symbol)` in a loop within an async coroutine. mt5linux RPyC calls are blocking network I/O. This **blocks the entire event loop**, freezing all workers while prices are fetched sequentially.

#### 4. No Graceful Shutdown
`asyncio.run(manager.start())` with `KeyboardInterrupt` catches the signal but never calls `manager.stop()`. Tasks are never cancelled. Database sessions may be left open. MT5 connection is never shut down.

#### 5. Price Feed Uses `symbol_info()` Not `symbol_info_tick()`
`mt5.symbol_info()` returns contract specification data (lot size, digits, etc.), NOT the current price tick. The code does `getattr(tick, 'bid', 0.0)` on a SymbolInfo object which doesn't have a `bid` attribute in the expected format. **Prices will always be 0.0 or wrong.**

#### 6. State Machine Has No Transition Validation
Any state can transition to any other state. There's no guard preventing `TP_REACHED → TRADE_OPEN` or `RISK_EXHAUSTED → WAITING_FOR_REENTRY`. A single bug creates impossible states.

### Hidden Assumptions
- Single process, single machine
- SQLite is adequate (it is not for concurrent async writers)
- MT5 connection is always fast
- Price feed never stalls
- Workers and API share the same database file

### Score: 4/10

---

# Quantitative Review

## What it attempts
A re-entry strategy with whipsaw recovery trailing that manages risk at the idea level rather than the trade level.

## Correctness Assessment: 3/10

### Is the Trade Idea concept sound?
**Conceptually yes, but with critical caveats.**

The idea of budgeting risk across multiple entry attempts rather than per-trade is used in institutional systematic trading. It maps to the concept of a "trade campaign" or "thesis." The specification correctly identifies that a single entry point with a fixed stop is often suboptimal for multi-day directional trades.

**However:**

### Critical Quantitative Issues

#### 1. No Statistical Evidence Required Before Deployment
The specification mentions backtesting but provides no framework for:
- Minimum sample size requirements
- Out-of-sample validation
- Walk-forward analysis
- Monte Carlo simulation
- Statistical significance testing

**Risk:** The entire strategy could be curve-fit to historical data with zero predictive power.

#### 2. Re-entry Zone Is Statically Defined
The entry zone is `entry ± 0.1%` (hardcoded in `server.py` line 60). This is:
- Too tight for XAUUSD (±$1 on a $2400 instrument = 0.04%)
- Too wide for NAS100 
- Not based on any volatility measure (ATR, realized vol, etc.)
- Identical for all market conditions

**Risk:** The system will either never re-enter (zone too tight) or re-enter into noise (zone appropriate by accident).

#### 3. Trailing Recovery Formula Has No Theoretical Justification
The progressive trailing formula `secure = consumed_risk + 60% × (profit - consumed_risk)` is arbitrary. Why 60%? The spec examples show different ratios. There's no optimization, no sensitivity analysis, no proof this improves expectancy versus a simple breakeven trail or ATR-based trail.

**Specific concern:** At 60% progressive securing, the trailing stop will be very tight relative to profit. For a volatile instrument like XAUUSD with 3-point retracements being normal, this guarantees premature exit on the winning trade.

Example: Entry 2400, consumed_risk=3, price=2410 (profit=10).
Secure = 3 + 0.6×7 = 7.2. Stop at 2407.2. 
A normal 3-point pullback to 2407 stops you out at +7.2 instead of potentially +30.
**The trail is designed to kill the winning trade that is supposed to recover all losses.**

#### 4. Whipsaw Detection Is Absent
The system has no concept of *detecting* whipsaw conditions. It mechanically re-enters whenever price returns to the zone. In a ranging market, this will:
1. Enter long → stopped out (loss 1)
2. Re-enter long → stopped out (loss 2)  
3. Re-enter long → stopped out (loss 3)
4. Exhaust risk budget
5. Price then rallies without the system

There's no ranging/trending filter, no volatility regime detection, no momentum confirmation.

#### 5. Position Sizing Uses `max_idea_risk` Not Remaining Risk
Line 131-133 of `main.py`: `risk_amount=idea.max_idea_risk`. On re-entry, the system sizes the position as if the full risk budget is available. If max_idea_risk=4.0 and consumed_risk=3.0, the system still sizes for 4.0 of risk. **This means the remaining 1.0 of budget will be consumed in a fraction of the expected stop distance, or the system will exceed its own risk budget.**

#### 6. No Slippage Model
All calculations assume fills at exact prices. For XAUUSD during NFP, slippage can be 5-10 points. The consumed_risk tracking becomes unreliable.

### What Must Be Backtested Before Deployment
1. Re-entry success rate by attempt number (does attempt #3 have positive expectancy?)
2. Progressive trailing impact on average winner size vs. simple TP
3. Risk budget exhaustion rate across market regimes
4. Optimal entry zone size per instrument (ATR-normalized)
5. Session filter impact on fill quality and expectancy
6. Correlation between whipsaw count and eventual trend capture probability

### Survivorship/Selection Bias Risks
- Only winners that eventually catch the trend appear profitable
- Losers that exhaust budget contribute hidden drag
- Backtest will show re-entry "works" because you selected trends that eventually materialized
- No framework to measure how often the trend simply never comes

### Score: 3/10

---

# Risk Review

## What it attempts
Multi-layer risk: idea-level (consumed_risk, max_retries), portfolio-level (daily loss limit, max active ideas, max account risk), execution-level (session filter, rollover filter).

## Correctness Assessment: 3/10

### Critical Risk Failures

#### 1. Daily Loss Calculation Is Completely Wrong
`portfolio.py` line 44-47:
```python
total_loss_today = db.query(TradeIdea).filter(
    TradeIdea.updated_at >= today,
    TradeIdea.realized_pnl < 0
).count() * 10
```
This counts the *number* of losing ideas updated today and multiplies by a magic number 10. It does **not** sum actual realized losses. If you have 5 ideas each losing $50 = $250 real loss, but `count() * 10 = 50`, the system thinks you're fine.

#### 2. `max_account_risk_percent` Is Never Checked
The parameter exists in the constructor but `can_accept_idea()` never reads account balance or checks total exposure against it. It's dead code creating false security.

#### 3. Consumed Risk Is Never Validated Before Re-entry
In `main.py` line 128-141, when `idea.state in [WAITING_FOR_SETUP, WAITING_FOR_REENTRY]` and price is in zone, the system enters **without checking**:
- `idea.consumed_risk < idea.max_idea_risk`
- `idea.retries_used < idea.max_retries`
- `idea.expires_at` hasn't passed

**All three spec requirements are violated.** The system will retry indefinitely until manually stopped.

#### 4. No Correlation Protection
The spec mentions `max_correlated_positions: 2` but there's no implementation. Buying EURUSD, GBPUSD, AUDUSD, and NZDUSD simultaneously means 4x USD-short exposure. During a USD rally, all four ideas hit stops simultaneously, consuming 4× the expected daily loss.

#### 5. Hard Stop Is Mutated by Trailing Logic
The `idea.hard_stop` field serves dual purpose: initial stop loss AND trailing stop level. Once the trailing engine moves it, the original risk reference is lost. If the trade is exited and re-entered, the "hard_stop" is now the trailed level, not the original safety stop. **Re-entries will have incorrect stop distances.**

#### 6. No Maximum Drawdown Circuit Breaker
No mechanism exists to halt all trading if account equity drops below a threshold. A cascading failure across correlated positions during a flash crash would drain the account.

#### 7. Position Sizing Ignores Contract Specifications
`calculate_lot_size` uses a default `tick_value=1.0`. For forex, the actual tick value depends on the pair, account currency, and current exchange rate. For XAUUSD, tick_value ≈ $0.01 per 0.01 lot per point. The default of 1.0 will produce position sizes that are orders of magnitude wrong.

### What Happens During Market-Wide Events
- **Flash Crash:** All correlated positions hit stops simultaneously. No circuit breaker. Daily loss limit is miscalculated. System continues accepting new ideas.
- **Central Bank Announcement:** Gaps through stops. consumed_risk based on original_entry not actual exit price. Risk budget appears available when it's actually exhausted.
- **Broker Disconnection During NFP:** Positions open with no monitoring. Trailing stops are software-only (not MT5 server-side stops). Total loss unbounded until reconnection.

### Score: 3/10

---

# Database Review

## What it attempts
Four-table schema: trade_ideas, trade_attempts, trade_events, open_positions. SQLAlchemy ORM with version columns for optimistic locking.

## Correctness Assessment: 4/10

### Missing Fields

| Table | Missing Field | Impact |
|---|---|---|
| trade_ideas | `lot_size` / `quantity` | Can't reconstruct position size after crash |
| trade_ideas | `current_attempt_entry` | Can't correctly calculate loss on current attempt |
| trade_ideas | `signal_fingerprint` | Duplicate detection uses in-memory dict, lost on restart |
| trade_attempts | `execution_state` | No tracking of PENDING/FILLED/REJECTED lifecycle |
| trade_attempts | `slippage` | Can't audit execution quality |
| trade_attempts | `actual_stop` / `actual_tp` | Trailing moves hard_stop but attempt-level stops aren't tracked |
| open_positions | `quantity` / `volume` | Can't reconstruct position size |
| open_positions | `unrealized_pnl` | No periodic mark-to-market |
| trade_events | `worker_id` | Can't trace which worker generated the event |
| ALL | `created_by` | No audit of who/what created each record |

### Missing Indexes

```sql
-- Critical for worker queries (run every 100ms per symbol)
CREATE INDEX idx_ideas_symbol_state ON trade_ideas(symbol, state);

-- Critical for daily loss calculation
CREATE INDEX idx_ideas_updated_pnl ON trade_ideas(updated_at, realized_pnl);

-- Critical for attempt lookups
CREATE INDEX idx_attempts_idea_id ON trade_attempts(trade_idea_id);

-- Critical for event queries
CREATE INDEX idx_events_idea_id ON trade_events(trade_idea_id);
CREATE INDEX idx_events_type ON trade_events(event_type);
```

Without these, every worker tick hits a full table scan. With 100 symbols polling every 100ms, that's 1000 unindexed queries per second.

### Missing Constraints

```sql
-- Direction must be BUY or SELL
CHECK (direction IN ('BUY', 'SELL'))

-- State must be valid enum value
CHECK (state IN ('WAITING_FOR_SETUP', ...))

-- Risk values must be positive
CHECK (max_idea_risk > 0)
CHECK (max_retries > 0)
CHECK (consumed_risk >= 0)

-- Entry zone must be ordered
CHECK (entry_zone_low < entry_zone_high)

-- Stop must be on correct side
-- For BUY: hard_stop < original_entry
-- For SELL: hard_stop > original_entry
```

### Data Consistency Issues

1. **Version column never used for optimistic locking.** The code increments `version` but never uses `WHERE version = expected_version` in updates. Two workers could theoretically modify the same idea.

2. **`realized_pnl` never updated.** The field exists but no code path writes to it. All PnL data is lost.

3. **`open_positions` table never written to.** No code creates, updates, or queries OpenPosition records. Crash recovery cannot work.

4. **`trade_events` table never written to.** No code creates TradeEvent records. The entire audit trail is nonexistent.

5. **`trade_attempts` table never written to.** No code creates TradeAttempt records. The idea-to-attempt relationship exists only in the ORM definition.

6. **`datetime.utcnow` is deprecated** in Python 3.12+. Should use `datetime.now(UTC)`.

### Recovery Weaknesses
On crash recovery, the system loads active ideas and creates workers, but:
- Open positions in MT5 are never reconciled against the database
- Trailing stop state is lost (hard_stop may have been modified but the context of why is gone)
- Consumed risk may be inaccurate if a crash occurred mid-exit
- No WAL mode configured for SQLite crash safety

### Score: 4/10
