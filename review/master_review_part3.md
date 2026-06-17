# Master Review — Part 3 of 3

# Production Readiness Score

| Subsystem | Score | Status |
|---|---|---|
| Trade Idea Lifecycle | 3/10 | States defined but transitions broken, no validation, no budget checks |
| Re-entry Logic | 2/10 | Entry zone detection works, but no guards (retries, risk, expiry) |
| Whipsaw Recovery Trailing | 5/10 | Formula implemented correctly, but never reaches MT5 |
| Portfolio Risk Manager | 2/10 | Daily loss calculation wrong, max_account_risk dead code |
| Position Sizing | 2/10 | Formula exists but tick_value default makes it useless |
| Worker Architecture | 3/10 | Structure correct, implementation fragile |
| Database Schema | 4/10 | Tables defined, 3 of 4 never written to |
| MT5 Bridge | 2/10 | Connect and place_order only. No close, no modify, no reconnect |
| Execution Queue | 4/10 | Queue pattern correct, no result handling |
| API/Signal Gateway | 5/10 | Functional for ingestion, duplicate detection not persistent |
| Backtesting | 0/10 | Nonexistent |
| Crash Recovery | 1/10 | Loads ideas from DB, no MT5 reconciliation |
| Monitoring/Alerting | 0/10 | Nonexistent |
| Session/Liquidity Filters | 4/10 | Implemented but incomplete symbol coverage |
| Audit Trail | 0/10 | TradeEvent table defined, never populated |
| **Overall** | **2.5/10** | **Not suitable for any deployment** |

## Production Readiness by Deployment Level

### Demo Account
**Blockers:** 6 critical

1. Fix `symbol_info()` → `symbol_info_tick()` (prices are wrong)
2. Implement position close orders
3. Implement MT5 stop modification (for trailing)
4. Add re-entry budget checks (retries, consumed risk, expiry)
5. Fix loss calculation to use actual attempt entry price
6. Capture and store order result/ticket

**Required changes:** ~15 code modifications

### Small Live Account ($500-$2000)
**Additional blockers beyond Demo:**

1. Fix daily loss limit calculation (sum PnL, not count×10)
2. Implement max_account_risk_percent check with real account balance
3. Fix position sizing tick_value per instrument
4. Add MT5 position reconciliation on startup
5. Add connection health monitoring and reconnection
6. Implement TradeAttempt and TradeEvent recording
7. Add input validation (stop on correct side, positive values)
8. Run blocking MT5 calls in executor (`loop.run_in_executor`)
9. Switch to WAL mode for SQLite
10. Add dynamic worker registration for new symbols

**Required changes:** ~30 code modifications, ~5 new modules

### Medium Live Account ($5000-$25000)
**Additional blockers beyond Small:**

1. Implement backtesting engine with statistical validation
2. Demonstrate positive expectancy with >500 out-of-sample trades
3. Implement correlation protection
4. Implement maximum drawdown circuit breaker
5. Add comprehensive monitoring (Prometheus/Grafana or equivalent)
6. Add alerting (email/Telegram for critical events)
7. Switch to PostgreSQL
8. Add proper logging with structured output
9. Implement gap handling
10. Implement weekend rules

### Professional Deployment
**Additional blockers beyond Medium:**

1. Multi-process worker distribution
2. Hardware watchdog process
3. Independent risk monitoring service
4. Real-time position reconciliation loop
5. Automated failover
6. Compliance audit trail
7. Performance profiling and optimization
8. Disaster recovery plan
9. Formal specification verification

---

# Requirement Traceability Matrix

## idea1.md — Core Trade Idea Spec

| Requirement | Status | Notes |
|---|---|---|
| Manage trade ideas not individual trades | Partial | Idea concept exists but attempt tracking broken |
| Multi-symbol support | Partial | Works only for symbols with pre-existing ideas at startup |
| Long-running trades (1-5 days) | Missing | No expiry enforcement, no weekend handling |
| Controlled re-entry after early exit | **DANGEROUS** | Re-enters without checking budget/retries/expiry |
| Cumulative loss tracking | **INCORRECT** | Uses original_entry not actual attempt entry for loss calc |
| Stop retrying on risk budget exhaustion | **MISSING** | No budget check before re-entry |
| Trailing recovery of whipsaw losses | Partial | Formula correct, never sent to MT5 |
| Configurable per instrument | Partial | YAML config mentioned in spec, hardcoded in implementation |
| WAITING_FOR_SETUP state | Complete | Implemented |
| ENTRY_ZONE_REACHED state | Missing | State exists in enum, never used in transitions |
| TRADE_OPEN state | Partial | Transition works, no actual MT5 verification |
| EARLY_EXIT state | Missing | State exists in enum, never used |
| WAITING_FOR_REENTRY state | Partial | Transition works, no guards |
| TP_REACHED state | Partial | Detected but position never closed |
| RISK_EXHAUSTED state | Partial | Only triggered from stop hit, not from re-entry guard |
| IDEA_EXPIRED state | Missing | Never checked or triggered |
| Entry zone check | Complete | Zone bounds checked correctly |
| Hard stop detection | Partial | Detected but no close order sent |
| Take profit detection | Partial | Detected but no close order sent |
| Retry budget (max_retries) | **MISSING** | Never checked before re-entry |
| Risk budget (max_idea_risk) | **MISSING** | Never checked before re-entry |
| Dynamic trailing stop | Partial | Calculates correctly, DB-only, no MT5 |
| Progressive trailing | Partial | Formula implemented, aggressive parameterization |
| Idea expiry | Missing | expires_at field exists, never evaluated |
| 100+ simultaneous ideas | Untested | Architecture supports it, SQLite may not |
| SQLite persistence | Complete | Schema created, most tables unused |
| Logging of all events | **MISSING** | Only logger.info, no TradeEvent records |
| Crash recovery | Partial | Loads ideas, no MT5 reconciliation |
| Paper trading mode | Missing | No simulation adapter |
| Live trading mode | Partial | MT5 bridge exists, incomplete |
| Async architecture | Partial | Async structure correct, blocking MT5 calls |
| Unit testable | Partial | 3 tests exist, minimal coverage |

## idea2.md — Signal Ingestion

| Requirement | Status | Notes |
|---|---|---|
| Manual user input | Missing | No CLI or manual interface |
| Telegram bot | Missing | No implementation |
| REST API | Complete | FastAPI endpoint works |
| Internal Python service | Partial | Can call API programmatically |
| Signal normalization | Complete | Pydantic model validates input |
| trade_ideas table | Complete | Schema matches spec |
| trade_attempts table | Complete (schema) | Table exists, **never populated** |
| trade_events table | Complete (schema) | Table exists, **never populated** |
| open_positions table | Complete (schema) | Table exists, **never populated** |
| Duplicate signal protection | Partial | In-memory only, lost on restart |
| Recovery on startup | Partial | Loads ideas, no full reconstruction |
| Analytics queries | Missing | No analytics implementation |

## idea3.md — Worker Architecture

| Requirement | Status | Notes |
|---|---|---|
| One worker per symbol | Complete | Correctly implemented |
| Worker isolation (no shared state) | Partial | Shared price_cache dict, shared execution queue |
| Async workers (asyncio) | Complete | create_task per symbol |
| Worker lifecycle | Partial | Start works, no proper shutdown |
| Symbol state cache | Partial | SymbolState dataclass exists, not fully used |
| Price feed service | **INCORRECT** | Uses symbol_info() not symbol_info_tick() |
| Execution queue | Complete | asyncio.Queue implemented |
| Trade idea assignment to worker | Complete | By symbol match |
| Restart recovery | Partial | Loads ideas, creates workers, no MT5 sync |
| Future scaling (multi-process) | Missing | Single process only, shared dict prevents distribution |

## idea4.md — Production Readiness & Edge Cases

| Requirement | Status | Notes |
|---|---|---|
| IDEA_INVALIDATED state | Partial | Enum value exists, no trigger mechanism |
| One active idea per symbol | Complete | Checked in PortfolioRiskManager |
| Position sizing engine | **DANGEROUS** | tick_value default=1.0 makes output wrong |
| Daily loss limit | **INCORRECT** | count×10 instead of sum(realized_pnl) |
| Max concurrent ideas | Complete | Checked in PortfolioRiskManager |
| Max account risk percent | **DEAD CODE** | Parameter exists, never evaluated |
| Correlation protection | Missing | Not implemented |
| Re-entry confirmation rules | Missing | No momentum/volume/trend confirmation |
| Entry zone logic | Complete | Zone calculation works |
| Gap beyond TP handling | Missing | No special gap logic |
| Gap beyond stop handling | Missing | No special gap logic |
| Weekend rules | Missing | No implementation |
| Duplicate signal protection | Partial | In-memory dict, not persistent |
| Execution state machine | Partial | Enum defined, states never used |
| Trailing recovery formula | Complete | Correctly implements spec formula |
| Progressive trailing | Complete | Percentage-based progressive protection |
| MT5 connectivity recovery | Missing | No DEGRADED/DISCONNECTED handling |
| Database version column | Partial | Column exists, optimistic locking not enforced |
| Backtesting engine | Missing | No implementation |
| Backtesting metrics | Missing | No implementation |
| Dashboard metrics | Missing | No implementation |
| Signal source priority | Missing | No priority logic |
| Full crash recovery | Missing | No MT5 reconciliation, no trailing state recovery |
| Audit trail | Missing | TradeEvent never populated |

## idea5.md — Whipsaw Recovery Trailing

| Requirement | Status | Notes |
|---|---|---|
| Consumed risk definition | Complete | Sum of realized losses (concept correct) |
| Recovery rule (profit > consumed_risk → secure) | Complete | Correctly implemented in TrailingStopEngine |
| Progressive profit protection | Complete | 60% progressive securing implemented |
| Priority: Recovery → Protect → Maximize → TP | Partial | Recovery and protect work. No TP-approach tightening |
| Never move stop backwards | Complete | Guard implemented correctly |
| **Critical gap:** Trail never reaches MT5 | **DANGEROUS** | DB-only trailing creates false safety |

---

# Final Verdict

## **Major Redesign Required**

### Justification

The system has a **sound architectural concept** — managing trade ideas as first-class entities with retry budgets and recovery trailing is a legitimate approach to systematic trend capture. The specification documents are thoughtful and cover most edge cases that matter.

However, the implementation is a **dangerously incomplete prototype** masquerading as a functional system. Specifically:

**It cannot lose money correctly:**
- Losses are calculated from the wrong price
- Risk budgets are never checked before re-entry
- Positions are never actually closed via MT5

**It cannot make money correctly:**
- Trailing stop adjustments never reach the broker
- Take profit hits are detected but positions aren't closed
- Position sizing produces absurd lot sizes with default parameters

**It cannot recover from failure:**
- No MT5 position reconciliation on startup
- Orphaned positions will accumulate
- Database state and broker state will diverge permanently after any interruption

**It has no observability:**
- Zero operational metrics
- Zero alerting
- 3 of 4 database tables are never written to
- No audit trail despite the spec requiring one

**It has no quantitative foundation:**
- No backtesting framework
- No statistical validation
- No evidence the re-entry strategy has positive expectancy
- Progressive trailing percentage is arbitrary

### What is salvageable
1. The **specification documents** are solid and form a good foundation
2. The **TrailingStopEngine** formula is correctly implemented
3. The **SQLAlchemy models** are a reasonable starting point
4. The **async worker pattern** is architecturally correct
5. The **FastAPI ingestion gateway** is functional

### Critical path to Demo deployment
1. Fix the MT5 bridge (close, modify, reconnect, use symbol_info_tick)
2. Implement order result capture and TradeAttempt creation
3. Add re-entry guard checks (budget, retries, expiry)
4. Fix loss calculation to use actual attempt entry price
5. Send trailing stop updates to MT5 via position modify
6. Fix position sizing with real tick values
7. Fix daily loss limit calculation
8. Add startup MT5 position reconciliation
9. Run blocking MT5 calls via `run_in_executor`
10. Implement backtesting engine and prove positive expectancy

**Estimated effort to reach Demo-ready:** 3-4 weeks of focused development
**Estimated effort to reach Small Live Account:** 6-8 weeks
**Estimated effort to reach Professional:** 4-6 months

---

*Review conducted by simulated panel. All scores reflect the state of the codebase at time of review. No score should be interpreted as a judgment of the designer's capability — the specification quality significantly exceeds the implementation quality, suggesting the gap is a matter of development stage, not design competence.*
