# Master Review — Part 2 of 3

# Worker Review

## What it attempts
One async coroutine per symbol, polling at 100ms, reading from a shared price cache, writing state to SQLite.

## Correctness Assessment: 3/10

### Critical Worker Failures

#### 1. Database Session Per Tick
Every 100ms, each worker opens a new session, queries the database, potentially commits, and closes. With 100 symbols, that's 1000 session open/close cycles per second, each with a query. SQLite will bottleneck hard.

#### 2. Worker Only Processes First Matching Idea
`session.query(...).first()` — if multiple ideas exist for a symbol (e.g., one WAITING_FOR_SETUP and one TRADE_OPEN), only the first one returned by SQLite is processed. The second is silently ignored.

#### 3. No Expiry Check
The worker never checks `idea.expires_at`. Ideas live forever regardless of the configured expiry. Stale ideas from weeks ago will still attempt re-entries.

#### 4. State Transition WAITING_FOR_SETUP → TRADE_OPEN Skips ENTRY_ZONE_REACHED
The spec defines `ENTRY_ZONE_REACHED` as an intermediate state. The implementation jumps directly from `WAITING_FOR_SETUP` to `TRADE_OPEN`, skipping it entirely.

#### 5. No EARLY_EXIT State Used
The spec defines `EARLY_EXIT` as a state. The implementation goes directly from `TRADE_OPEN` to `WAITING_FOR_REENTRY`, never recording the intermediate exit state.

#### 6. Loss Calculation Bug (Repeated — Critical)
```python
loss = abs(idea.original_entry - current_price)
```
This calculates loss from the *original* entry, not the *current attempt's* entry. If the first entry was at 100, stopped at 98 (loss=2), then re-entry at 99, stopped at 97, the system calculates `abs(100-97)=3` instead of `abs(99-97)=2`. **Consumed risk is systematically overstated**, causing premature RISK_EXHAUSTED.

#### 7. No Actual MT5 Position Closing
When a stop hit or TP hit is detected, the system changes the database state but **never sends a close order to MT5**. The position remains open on the broker while the system believes it's closed.

#### 8. Race Condition: Price Feed vs Worker
The price feed loop and workers share `shared_price_cache` (a plain dict) without any synchronization. In CPython this is *probably* safe due to the GIL for simple dict operations, but:
- Reading a stale price is guaranteed (up to 500ms stale)
- If migrated to multi-process (as the spec envisions), this breaks completely

#### 9. No Heartbeat or Health Check
No mechanism to detect if a worker is stuck, deadlocked, or throwing exceptions in a loop. The `except Exception` swallows all errors silently (logs them but continues).

### Can Race Conditions Occur?
**Yes.** The API server can modify ideas (create new ones) while workers read them. With SQLite and no row-level locking, a worker could read a partially-committed idea. The version column exists but optimistic locking is not implemented.

### Can State Corruption Occur?
**Yes.** If the process crashes between `idea.state = TradeState.TRADE_OPEN` (line 139) and `session.commit()` (line 141), no corruption. But if the crash occurs after `session.commit()` but before the execution queue processes the order, the database says TRADE_OPEN but no MT5 position exists.

### Can Duplicate Execution Occur?
**Yes.** If the execution queue is slow and the worker loops again in 100ms, it will see the idea is already TRADE_OPEN and skip. But if the commit didn't happen fast enough (unlikely with SQLite but possible with PostgreSQL), the worker could queue a second execution request.

### Score: 3/10

---

# MT5 Execution Layer Review

## What it attempts
A bridge wrapping mt5linux RPyC calls for initialization, price queries, and order placement.

## Correctness Assessment: 2/10

### Critical Execution Failures

#### 1. No Position Close Function
The bridge can open positions (`place_order`) but has **no method to close them**. The entire exit pathway is unimplemented. This alone makes the system non-functional for live trading.

#### 2. No Stop Modification Function
The trailing stop engine calculates new stop levels, which get written to `idea.hard_stop` in the database, but **never sent to MT5**. The actual MT5 position retains its original stop. The trailing stop is a database fiction.

#### 3. Order Result Ignored
`main.py` line 93-96: `self.bridge.place_order(...)` — the return value is not captured. No ticket number is stored. No fill confirmation. No rejection handling. The system doesn't know if the order worked.

#### 4. No Retry Logic
If `order_send` fails, the bridge logs an error and returns None. There is no retry mechanism. A transient MT5 error (requote, timeout) permanently loses the trade.

#### 5. No Connection State Management
`self.connected` is set once at startup. If MT5 disconnects mid-session, `self.connected` remains True. All subsequent calls silently fail or throw exceptions caught by the worker's generic handler. The DEGRADED/DISCONNECTED states from the spec are unimplemented.

#### 6. Hardcoded Magic Number and Deviation
`magic: 234000` and `deviation: 20` are hardcoded. Different strategies should have different magic numbers. Deviation of 20 may be too tight for volatile instruments (XAUUSD during news) or too loose for majors.

#### 7. Filling Type Hardcoded to IOC
`ORDER_FILLING_IOC` (Immediate-or-Cancel) will cancel unfilled portions. For the lot sizes in this system (0.01-0.1), this is probably fine, but for larger sizes it could result in partial fills that are never detected or handled.

#### 8. `symbol_info()` vs `symbol_info_tick()`
As noted in Architecture Review, the price feed uses `mt5.symbol_info()` which returns contract specs, not price data. Should be `mt5.symbol_info_tick()`.

### What Happens During Disconnects?
- Trailing stops stop updating (database only, never sent to MT5 anyway)
- No new entries (bridge returns None, swallowed by exception handler)
- Existing positions run unmonitored with only the original MT5 hard stop
- No reconnection attempt
- No alerting

### What Happens During Partial Fills?
Not handled. System assumes full fill or full rejection.

### What Happens During Execution Delays?
The execution queue blocks on `await self.execution_queue.get()`. If MT5 is slow, subsequent orders queue up. There's no timeout, no queue depth monitoring, no stale-order rejection.

### Score: 2/10

---

# Backtesting Review

## What it attempts
The spec calls for a backtesting engine with adapter pattern. **Zero implementation exists.**

## Correctness Assessment: 0/10

### What's Missing (Everything)
1. No backtesting engine
2. No adapter pattern for strategy code
3. No historical data loading
4. No simulated execution
5. No slippage model
6. No spread model
7. No fill simulation
8. No metric calculation
9. No reporting

### Unrealistic Assumptions If Naively Implemented
1. **Fill at exact prices** — In reality, limit orders may not fill, market orders slip
2. **Stops execute at exact price** — Gaps through stops are common (spec acknowledges but implementation ignores)
3. **No spread impact** — Entry/exit costs not modeled. For EURUSD at 0.1 pip spread and 5 re-entries, that's 1.0 pip of hidden cost per idea
4. **Trailing stop moves instantly** — Real trailing requires MT5 modify calls with latency
5. **Re-entry is instantaneous** — In reality, by the time you detect zone return and place order, price may have left the zone
6. **No market impact** — For small lot sizes this is fine; for scaling up, it matters
7. **No data quality validation** — Missing ticks, incorrect timestamps, corporate actions

### Look-Ahead Bias Risks
- Entry zone calculation uses the entry price which is the "signal" — but in a backtest, the signal is generated from data that already shows the entry price was reached
- The session filter uses `datetime.utcnow()` — in a backtest, this would use real time, not simulated time

### Recommended Validation Framework
1. **Walk-Forward Optimization**: 70/30 in-sample/out-of-sample split, rolled quarterly
2. **Monte Carlo Permutation Test**: Shuffle trade sequence 10,000 times, verify edge survives
3. **Minimum 500 trades** per configuration before statistical validity
4. **Sharpe Ratio > 1.5** after transaction costs
5. **Max Drawdown < 20%** of account
6. **Profit Factor > 1.3** out-of-sample
7. **t-stat > 2.0** on per-trade returns

### Score: 0/10

---

# Missing Requirements

| Requirement | Spec Document | Status |
|---|---|---|
| Telegram Bot Integration | idea2.md | Completely Missing |
| Signal Fingerprint (persistent) | idea4.md | Missing (in-memory only, lost on restart) |
| Idea Invalidation (IDEA_INVALIDATED) | idea4.md | State exists but no trigger logic |
| Position Sizing from Account Balance | idea4.md | Missing (no account balance query) |
| Gap Handling (beyond TP) | idea4.md | Missing |
| Gap Handling (beyond Stop) | idea4.md | Partially implemented (no actual close) |
| Weekend Rules | idea4.md | Missing |
| Execution State Machine | idea4.md | Enum defined, never used |
| MT5 Connectivity Recovery | idea4.md | Missing (DEGRADED/DISCONNECTED) |
| Backtesting Engine | idea4.md | Missing |
| Dashboard Metrics | idea4.md | Missing |
| Analytics Queries | idea2.md | Missing |
| Crash Recovery (full) | idea2.md, idea4.md | Partial (loads ideas, no MT5 reconciliation) |
| Position Close Orders | idea1.md | Missing |
| Stop Modification Orders | idea1.md, idea5.md | Missing |
| TradeAttempt Creation | idea2.md | Missing |
| TradeEvent Logging | idea2.md | Missing |
| OpenPosition Management | idea2.md | Missing |
| Correlation Protection | idea4.md | Missing |
| Max Account Risk Check | idea4.md | Missing (dead code) |
| Paper Trading Mode | idea1.md | Missing |
| Multiple Broker Support | idea4.md | Missing (future, acceptable) |
| Re-entry Confirmation Rules | idea4.md | Missing |
| Signal Source Priority | idea4.md | Missing |

---

# Dangerous Assumptions

1. **MT5 stops are managed by the system, not the broker.** If the system crashes, stops exist only as software state, not as server-side orders that MT5 maintains. The hard_stop sent at order placement is the ONLY real protection.

2. **Trailing stop updates modify a database field but never reach MT5.** The broker has no knowledge of trail adjustments. If the system crashes after trailing, the original stop is all that protects the position.

3. **Order placement always succeeds.** No verification, no ticket tracking, no fill confirmation.

4. **Position closing happens automatically when stops are hit.** The system detects a price beyond the stop but never sends a close command. It relies on MT5's server-side stop — which was never updated by the trailing logic.

5. **Single-threaded async is sufficient.** MT5 RPyC calls are blocking and will freeze all workers.

6. **SQLite handles concurrent writes.** Multiple coroutines writing simultaneously will cause lock contention.

7. **0.1% entry zone is universally appropriate.** No volatility normalization.

8. **60% progressive trailing percentage is optimal.** No optimization or justification.

9. **All instruments behave identically.** Same trailing logic, same entry zone calculation for gold, forex, and indices.

10. **Daily loss limit of $50 is meaningful.** The calculation is wrong (count×10, not sum of PnL).

11. **`datetime.utcnow()` is reliable for trading decisions.** It's deprecated and doesn't handle timezone-aware comparisons correctly.

12. **Price cache staleness of 500ms is acceptable.** For XAUUSD moving $2/second during news, a 500ms stale price could mean a $1+ discrepancy.

---

# 25 Failure Scenarios

| # | Scenario | Impact | Likelihood |
|---|---|---|---|
| 1 | System crashes while position is open. Trailing stop was at +5 in DB but MT5 has original stop at -4. Market reverses. | Loss of 9 points instead of expected breakeven | HIGH |
| 2 | MT5 disconnects mid-trade. System continues "monitoring" with stale prices. Believes position is fine while it's being liquidated. | Unlimited loss until reconnection | HIGH |
| 3 | NFP release causes 50-pip gap. System detects stop hit, marks WAITING_FOR_REENTRY, but never closes position. Position continues losing. | Double the expected loss | HIGH |
| 4 | New idea submitted via API for USDCAD. No worker exists for USDCAD. Idea sits in WAITING_FOR_SETUP forever. | Missed trade opportunity, user confusion | HIGH |
| 5 | Five correlated USD-short ideas all hit stops during Fed announcement. Daily loss = 5 × $50 = $250. System calculates daily loss as count(5) × 10 = $50 < limit. Accepts new ideas. | Cascading losses beyond daily limit | HIGH |
| 6 | System places buy order. MT5 rejects (insufficient margin). Bridge returns None. System has already marked idea as TRADE_OPEN. Worker tries trailing on nonexistent position. | Phantom trade, corrupted state | HIGH |
| 7 | SQLite "database is locked" error during high-frequency updates. Session.commit() throws. Exception caught, idea state not persisted. Worker retries, resubmits order. | Duplicate positions | MEDIUM |
| 8 | Market opens with gap beyond TP. System detects TP hit, marks TP_REACHED, never closes. Position open at broker, profit erodes. | Unrealized profit becomes loss | HIGH |
| 9 | Trailing engine moves hard_stop to +7. Idea stops out. Re-enters. New attempt uses hard_stop of +7 as stop loss (above entry). Instant stop hit. | Immediate loss on re-entry, risk budget wasted | HIGH |
| 10 | VPS clock drifts 30 seconds. Session filter misidentifies active session. Entries placed during low-liquidity Asian session for EURUSD. | Poor fills, excessive slippage | MEDIUM |
| 11 | Worker exception handler catches every error. Worker continues looping with corrupted state. Logs fill disk. No alert sent. | Silent degradation, undetected losses | HIGH |
| 12 | Price feed uses `symbol_info()` instead of `symbol_info_tick()`. Returns 0.0 for price. All entry zone checks fail. No trades ever execute. | System appears functional but does nothing | HIGH |
| 13 | Two API requests arrive simultaneously for same symbol. Duplicate check uses in-memory dict. Under race condition, both pass. Two ideas created for same symbol. | Conflicting ideas, double risk | MEDIUM |
| 14 | Consumed risk = 3.9, max_idea_risk = 4.0. System enters (no budget check). Stop hit at -2.0. consumed_risk = 5.9, exceeding budget by 47%. | Risk budget meaningfully exceeded | HIGH |
| 15 | System restarts. Loads active ideas. MT5 has positions from before crash. No reconciliation. System creates new positions alongside orphaned old ones. | Double exposure, margin call | HIGH |
| 16 | Backtest implemented naively using system clock for session filter. All historical trades filtered through current time, not simulated time. Backtest results meaningless. | False confidence in strategy | HIGH (future) |
| 17 | Lot size calculation: risk=$10, stop=0.0050 (forex), tick_value=1.0 (default). lot = 10/0.005 = 2000 lots. Broker rejects or fills 2000 standard lots. | Catastrophic over-sizing or rejection | CRITICAL |
| 18 | Weekend gap. allow_weekend_holding not implemented. Position held through weekend. Sunday open gaps 200 pips against. Original stop far away. | Massive unexpected loss | MEDIUM |
| 19 | Idea created with entry=100, hard_stop=105 (SELL). Direction=BUY. No validation. System places BUY with stop above entry. Nonsensical trade. | Guaranteed loss on entry | MEDIUM |
| 20 | Execution queue backs up during volatility. 50 orders queued. Rollover period starts. First order gets re-queued. Queue grows. Memory exhaustion. | System crash, orphaned positions | LOW |
| 21 | Process killed with SIGKILL. `finally` blocks don't run. SQLite WAL not flushed. Database corruption. | Total state loss | LOW |
| 22 | mt5linux RPyC connection drops silently. `self.connected` stays True. All bridge calls throw exceptions. Workers catch them, log, continue. No recovery attempt. | System alive but non-functional | HIGH |
| 23 | User submits SELL idea. Entry zone calculated as `entry ± 0.1%`. For SELL, price must *fall into* zone. But zone is above current price. Entry condition may never trigger or trigger incorrectly. | Missed entry or wrong entry | MEDIUM |
| 24 | API server and TradeManager use different SQLite database files (different CWD at startup). Ideas created via API never seen by workers. | Complete operational failure | MEDIUM |
| 25 | Trailing stop oscillates: price ticks up → trail moves up → price ticks down (within spread) → no backward move → price ticks up slightly → trail moves again. Hundreds of DB writes per second on a winning trade. | SQLite lock contention, performance degradation | HIGH |
