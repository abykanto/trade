import asyncio
import logging
import json
import os
import time
from typing import Dict
from datetime import timedelta

from src.core.models import (
    TradeIdea, TradeState, SymbolState, ExecutionRequest,
    TradeAttempt, TradeEvent, OpenPosition, ExecutionState, utcnow
)
from src.core.database import init_db, get_session_local, as_utc
from src.core.config import load_trading_config, TradingConfig
from src.execution.bridge import MT5Bridge, ConnectionState
from src.execution.trailing import TrailingStopEngine
from src.risk.quant_filters import SessionManager, LiquidityFilter
from src.risk.portfolio import PortfolioRiskManager
from src.market import (
    MarketTick,
    SymbolContract,
    TradeLevels,
    resolve_position_ticket,
    position_direction,
)
from src.market.price_logger import XauusdPriceParquetLogger

logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                    handlers=[logging.FileHandler("trade_manager.log"), logging.StreamHandler()])
logger = logging.getLogger(__name__)

class TradeManager:
    def __init__(self, db_url="sqlite:///trade_ideas.db", config: TradingConfig | None = None):
        self.engine = init_db(db_url)
        self.SessionLocal = get_session_local(self.engine)
        self.running = False
        self.config = config or load_trading_config()
        
        self.symbol_states: Dict[str, SymbolState] = {}
        self.execution_queue = asyncio.Queue()
        
        self.bridge = MT5Bridge()
        self.session_manager = SessionManager()
        self.liquidity_filter = LiquidityFilter()
        self.trailing_engine = TrailingStopEngine(progressive_secure_percentage=0.6)
        self.risk_manager = PortfolioRiskManager(
            daily_dd_pct=float(os.environ.get("DAILY_DD_PCT", "0.03")),
            max_account_risk_percent=float(os.environ.get("MAX_ACCOUNT_RISK_PCT", "5.0")),
        )
        self.price_parquet_logger = XauusdPriceParquetLogger(
            flush_every_rows=int(os.environ.get("PRICE_LOG_FLUSH_ROWS", "1")),
        )
        
        self.tasks = []
        
    def _log_event(self, session, idea_id: int, event_type: str, data: dict = None):
        event = TradeEvent(
            trade_idea_id=idea_id,
            event_type=event_type,
            event_data=json.dumps(data) if data else None
        )
        session.add(event)

    @staticmethod
    def _final_hard_sl_breached(idea: TradeIdea, price: float) -> bool:
        return TradeLevels.final_hard_sl_breached(
            idea.direction, idea.original_hard_stop, price
        )

    @staticmethod
    def _take_profit_breached_before_entry(idea: TradeIdea, price: float) -> bool:
        return TradeLevels.take_profit_breached(idea.direction, idea.take_profit, price)

    @staticmethod
    def _pre_entry_sl_price(idea: TradeIdea, bid: float, ask: float) -> float:
        return MarketTick(bid, ask).pre_entry_sl_price(idea.direction)

    @staticmethod
    def _pre_entry_tp_price(idea: TradeIdea, bid: float, ask: float) -> float:
        return MarketTick(bid, ask).pre_entry_tp_price(idea.direction)

    @staticmethod
    def _open_trade_sl_price(direction: str, bid: float, ask: float) -> float:
        return MarketTick(bid, ask).open_trade_sl_price(direction)

    @staticmethod
    def _open_trade_tp_price(direction: str, bid: float, ask: float) -> float:
        return MarketTick(bid, ask).open_trade_tp_price(direction)

    @staticmethod
    def _resolve_position_ticket(
        positions,
        order_ticket: int,
        direction: str | None = None,
        volume: float | None = None,
        entry_price: float | None = None,
    ) -> int | None:
        return resolve_position_ticket(
            positions, order_ticket, direction, volume, entry_price
        )

    @staticmethod
    def _classify_stop_exit(
        direction: str,
        close_price: float,
        chop_sl: float,
        hard_stop: float,
        contract: SymbolContract,
    ) -> str:
        return TradeLevels.classify_stop_exit(
            direction, close_price, chop_sl, hard_stop, contract
        )

    async def _ensure_chop_stop_on_position(
        self,
        symbol: str,
        pos_ticket: int,
        chop_sl: float,
        tp: float,
        contract: SymbolContract,
    ) -> bool:
        """Confirm chop SL is live on the position; modify or emergency-close if not."""
        pos = await self.bridge.get_position(pos_ticket)
        if pos is None:
            return False

        if self.bridge.position_stop_matches(
            pos, chop_sl, contract.tick_size, contract.tick_value, contract.symbol
        ):
            return True

        mod_res = await self.bridge.modify_position(
            ticket=pos_ticket, symbol=symbol, sl=chop_sl, tp=tp
        )
        if mod_res is not None:
            return True

        pos = await self.bridge.get_position(pos_ticket)
        if pos and self.bridge.position_stop_matches(
            pos, chop_sl, contract.tick_size, contract.tick_value, contract.symbol
        ):
            return True

        pos_dir = position_direction(pos) if pos else None
        volume = getattr(pos, 'volume', 0.0) if pos else 0.0
        if pos_dir and volume > 0:
            logger.error(
                f"[{symbol}] Chop SL {chop_sl} could not be set on position "
                f"{pos_ticket} — emergency market close"
            )
            close_res = await self.bridge.close_position(
                ticket=pos_ticket, symbol=symbol,
                direction=pos_dir.value, volume=volume,
            )
            return close_res is not None
        return False

    async def _recover_or_block_reentry(
        self, session, idea: TradeIdea, symbol: str, positions: list
    ) -> bool:
        """If MT5 still has exposure, recover TRADE_OPEN or block a duplicate entry."""
        if not positions:
            return False

        attempt = session.query(TradeAttempt).filter_by(
            trade_idea_id=idea.id,
        ).order_by(TradeAttempt.id.desc()).first()
        if not attempt:
            return True

        pos_ticket = self._resolve_position_ticket(
            positions,
            attempt.mt5_ticket or 0,
            direction=idea.direction,
            volume=attempt.volume,
            entry_price=attempt.entry_price,
        )
        if pos_ticket is None:
            logger.warning(
                f"[{symbol}] {len(positions)} open MT5 position(s) block re-entry "
                f"for idea {idea.id}"
            )
            return True

        chop_sl = self.config.chop_exit_price(
            symbol, attempt.entry_price, idea.direction
        )
        idea.hard_stop = chop_sl
        attempt.execution_state = ExecutionState.FILLED.value
        idea.state = TradeState.TRADE_OPEN.value

        existing = session.query(OpenPosition).filter_by(trade_idea_id=idea.id).first()
        if existing:
            existing.mt5_ticket = pos_ticket
            existing.current_stop = chop_sl
        else:
            session.add(OpenPosition(
                trade_idea_id=idea.id,
                mt5_ticket=pos_ticket,
                symbol=symbol,
                direction=idea.direction,
                volume=attempt.volume,
                entry_price=attempt.entry_price,
                current_stop=chop_sl,
                current_tp=idea.take_profit,
            ))
        self._log_event(session, idea.id, "POSITION_RECOVERED", {
            "ticket": pos_ticket,
            "order_ticket": attempt.mt5_ticket,
            "chop_sl": chop_sl,
        })
        idea.version += 1
        session.commit()
        await self._ensure_chop_stop_on_position(
            symbol, pos_ticket, chop_sl, idea.take_profit,
            SymbolContract.for_symbol(symbol),
        )
        logger.info(
            f"[{symbol}] Recovered desynced position {pos_ticket} for idea {idea.id}"
        )
        return True

    def _invalidate_pre_entry_breach(self, session, idea: TradeIdea, price: float, sl_breached: bool):
        idea.state = TradeState.IDEA_INVALIDATED.value
        reason = "SL_HIT_BEFORE_ENTRY" if sl_breached else "TP_HIT_BEFORE_ENTRY"
        self._log_event(session, idea.id, "PRE_ENTRY_INVALIDATED", {"reason": reason, "price": price})
        idea.version += 1
        session.commit()
        logger.info(
            f"[{idea.symbol}] Idea {idea.id} invalidated: {reason} at {price} "
            f"(final hard SL={idea.original_hard_stop}, no fill required)"
        )

    async def _cancel_pending_and_invalidate_pre_entry(
        self, session, idea: TradeIdea, price: float, sl_breached: bool
    ) -> bool:
        """Cancel any untriggered MT5 limit/stop order, then kill the idea."""
        attempt = session.query(TradeAttempt).filter_by(
            trade_idea_id=idea.id,
            execution_state=ExecutionState.SUBMITTED.value,
        ).order_by(TradeAttempt.id.desc()).first()

        if attempt and attempt.mt5_ticket:
            cancel_res = await self.bridge.cancel_pending_order(attempt.mt5_ticket)
            if cancel_res is None:
                logger.warning(
                    f"[{idea.symbol}] Failed to cancel pending order {attempt.mt5_ticket} "
                    f"for idea {idea.id} — will retry next tick."
                )
                return False
            attempt.execution_state = ExecutionState.CANCELLED.value
            attempt.exit_reason = "PRE_ENTRY_BREACH"
            logger.info(
                f"[{idea.symbol}] Cancelled untriggered MT5 order {attempt.mt5_ticket} "
                f"— idea {idea.id} no longer valid past final SL {idea.original_hard_stop}"
            )

        self._invalidate_pre_entry_breach(session, idea, price, sl_breached)
        return True

    async def _handle_trade_closed(
        self,
        session,
        idea: TradeIdea,
        attempt: TradeAttempt,
        open_pos: OpenPosition,
        symbol: str,
        close_price: float,
        chop_sl: float,
        exit_reason: str,
        contract: SymbolContract,
        hard_stop_at_close: float | None = None,
        elapsed_time: timedelta | None = None,
    ):
        """Record a closed attempt and transition idea state after exit."""
        if exit_reason == "MT5_STOP":
            hard_stop = hard_stop_at_close if hard_stop_at_close is not None else idea.hard_stop
            exit_reason = self._classify_stop_exit(
                idea.direction, close_price, chop_sl, hard_stop, contract
            )

        pnl_dollars = contract.dollar_pnl(
            idea.direction, attempt.entry_price, close_price, attempt.volume
        )

        attempt.exit_price = close_price
        attempt.pnl = pnl_dollars
        attempt.closed_at = utcnow()
        attempt.exit_reason = exit_reason
        idea.realized_pnl += pnl_dollars
        session.delete(open_pos)

        target = self.trailing_engine.target_net_profit
        had_whipsaw = idea.consumed_risk > 0 or idea.retries_used > 0

        if exit_reason == "MAX_HOLD_EXCEEDED":
            idea.state = TradeState.IDEA_INVALIDATED.value
            self._log_event(session, idea.id, "TIME_EXIT", {
                "pnl": pnl_dollars,
                "elapsed_hours": elapsed_time.total_seconds() / 3600 if elapsed_time else 0,
            })
        elif exit_reason == "TAKE_PROFIT":
            idea.state = TradeState.TP_REACHED.value
            self._log_event(session, idea.id, "TP_REACHED", {"pnl": pnl_dollars})
        elif exit_reason == "TRAILING_STOP" and idea.realized_pnl >= target and had_whipsaw:
            idea.state = TradeState.TP_REACHED.value
            self._log_event(session, idea.id, "RECOVERY_TARGET", {
                "pnl": pnl_dollars,
                "idea_net": idea.realized_pnl,
                "target": target,
            })
        elif exit_reason == "CHOP_EXIT":
            if pnl_dollars < 0:
                idea.consumed_risk += abs(pnl_dollars)
            idea.retries_used += 1
            self._log_event(session, idea.id, "EARLY_EXIT", {
                "pnl": pnl_dollars,
                "consumed_risk": idea.consumed_risk,
                "exit_reason": exit_reason,
                "chop_sl": chop_sl,
            })
            if idea.consumed_risk >= idea.max_idea_risk or idea.retries_used >= idea.max_retries:
                idea.state = TradeState.IDEA_INVALIDATED.value
                reason = "RISK_EXHAUSTED" if idea.consumed_risk >= idea.max_idea_risk else "MAX_RETRIES"
                self._log_event(session, idea.id, "IDEA_INVALIDATED", {
                    "reason": reason, "consumed_risk": idea.consumed_risk
                })
            else:
                idea.state = TradeState.WAITING_FOR_REENTRY.value
                idea.hard_stop = idea.original_hard_stop
                self._log_event(session, idea.id, "WAITING_FOR_REENTRY", {
                    "remaining_risk": idea.max_idea_risk - idea.consumed_risk
                })
                # Brief pause so MT5 can flatten before re-entry
                await asyncio.sleep(1.0)
        elif exit_reason == "TRAILING_STOP":
            self._log_event(session, idea.id, "EARLY_EXIT", {
                "pnl": pnl_dollars,
                "consumed_risk": idea.consumed_risk,
                "exit_reason": exit_reason,
                "chop_sl": chop_sl,
            })
            idea.state = TradeState.WAITING_FOR_REENTRY.value
            idea.hard_stop = idea.original_hard_stop
            self._log_event(session, idea.id, "WAITING_FOR_REENTRY", {
                "remaining_risk": idea.max_idea_risk - idea.consumed_risk
            })
        else:
            self._log_event(session, idea.id, "EARLY_EXIT", {
                "pnl": pnl_dollars,
                "exit_reason": exit_reason,
            })
            idea.state = TradeState.IDEA_INVALIDATED.value

        idea.version += 1
        session.commit()

    _PRE_ENTRY_STATES = frozenset({
        TradeState.WAITING_FOR_SETUP.value,
        TradeState.WAITING_FOR_REENTRY.value,
        TradeState.PENDING_ORDER_PLACED.value,
        "SUBMITTING_ORDER",
    })

    async def start(self):
        self.running = True
        logger.info("Starting Modular Trade Manager...")
        
        if not await self.bridge.connect():
            logger.warning("Failed to connect to MT5. Will attempt reconnection in loop.")
            
        await self._reconcile_positions_on_startup()
        
        self.tasks.append(asyncio.create_task(self.price_feed_loop()))
        self.tasks.append(asyncio.create_task(self.order_executor_loop()))
        self.tasks.append(asyncio.create_task(self.worker_registration_loop()))
        self.tasks.append(asyncio.create_task(self._xauusd_price_logger_loop()))
        
        while self.running:
            if self.bridge.connection_state == ConnectionState.DISCONNECTED:
                await self.bridge.reconnect()
            await asyncio.sleep(5.0)

    async def stop(self):
        self.running = False
        for task in self.tasks:
            task.cancel()
        try:
            self.price_parquet_logger.flush()
        except Exception as exc:
            logger.warning("Price logger final flush failed: %s", exc)
        await self.bridge.shutdown()
        logger.info("Trade Manager stopping.")

    async def _xauusd_price_logger_loop(self):
        """Record XAUUSD bid/ask every second to Parquet for post-trade validation."""
        await self.price_parquet_logger.run_loop(
            bridge=self.bridge,
            interval_sec=float(os.environ.get("PRICE_LOG_INTERVAL_SEC", "1.0")),
            running_flag=lambda: self.running,
        )

    async def _reconcile_positions_on_startup(self):
        """Reconcile DB open positions with MT5 on startup."""
        if self.bridge.connection_state != ConnectionState.ACTIVE:
            return
            
        mt5_positions = await self.bridge.get_positions()
        mt5_tickets = {p.ticket: p for p in mt5_positions} if mt5_positions else {}
        
        with self.SessionLocal() as session:
            db_positions = session.query(OpenPosition).all()
            for db_pos in db_positions:
                if db_pos.mt5_ticket not in mt5_tickets:
                    logger.warning(f"Position {db_pos.mt5_ticket} missing in MT5. Marking closed.")
                    idea = session.query(TradeIdea).get(db_pos.trade_idea_id)
                    attempt = session.query(TradeAttempt).filter_by(
                        trade_idea_id=idea.id, execution_state=ExecutionState.FILLED.value
                    ).order_by(TradeAttempt.id.desc()).first()
                    
                    if attempt:
                        attempt.exit_price = attempt.entry_price
                        attempt.pnl = 0.0
                        attempt.exit_reason = "RECONCILIATION_MISSING"
                        attempt.closed_at = utcnow()
                        attempt.execution_state = ExecutionState.CANCELLED.value
                        
                    if idea and idea.state == TradeState.TRADE_OPEN.value:
                        idea.state = TradeState.IDEA_INVALIDATED.value
                        self._log_event(session, idea.id, "RECONCILIATION_INVALIDATED")
                        idea.version += 1
                        
                    session.delete(db_pos)
            
            # Catch stuck ideas from crash
            stuck_ideas = session.query(TradeIdea).filter_by(state="SUBMITTING_ORDER").all()
            for idea in stuck_ideas:
                logger.warning(f"Idea {idea.id} stuck in SUBMITTING_ORDER from previous crash. Invalidating.")
                idea.state = TradeState.IDEA_INVALIDATED.value
                self._log_event(session, idea.id, "CRASH_INVALIDATED")
                idea.version += 1
                
            session.commit()

    async def worker_registration_loop(self):
        """Periodically checks for new symbols with active ideas."""
        while self.running:
            try:
                with self.SessionLocal() as session:
                    active_ideas = session.query(TradeIdea).filter(
                        TradeIdea.state.in_([
                            TradeState.WAITING_FOR_SETUP.value,
                            TradeState.PENDING_ORDER_PLACED.value,
                            TradeState.TRADE_OPEN.value,
                            TradeState.WAITING_FOR_REENTRY.value
                        ])
                    ).all()
                    
                    symbols = set(idea.symbol for idea in active_ideas)
                    for symbol in symbols:
                        if symbol not in self.symbol_states:
                            self.symbol_states[symbol] = SymbolState(symbol=symbol)
                            task = asyncio.create_task(self.symbol_worker(symbol))
                            self.tasks.append(task)
                            logger.info(f"Registered new worker for {symbol}")
            except Exception as e:
                logger.error(f"Worker registration error: {e}")
            await asyncio.sleep(5.0)

    async def price_feed_loop(self):
        while self.running:
            if self.bridge.connection_state == ConnectionState.DISCONNECTED:
                await asyncio.sleep(1.0)
                continue
                
            try:
                for symbol in list(self.symbol_states.keys()):
                    tick = await self.bridge.get_tick(symbol)
                    if tick:
                        self.symbol_states[symbol].latest_bid = getattr(tick, 'bid', 0.0)
                        self.symbol_states[symbol].latest_ask = getattr(tick, 'ask', 0.0)
                        self.symbol_states[symbol].last_tick_time = time.monotonic()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Price feed error: {e}")
            await asyncio.sleep(0.5)

    async def order_executor_loop(self):
        while self.running:
            try:
                req: ExecutionRequest = await self.execution_queue.get()
                
                if self.liquidity_filter.is_rollover_period():
                    logger.warning(f"OrderExecutor: Holding execution for {req.symbol} due to Rollover trap.")
                    await asyncio.sleep(1.0)
                    await self.execution_queue.put(req)
                    self.execution_queue.task_done()
                    continue
                
                logger.info(f"Executing pending order for {req.symbol}")
                live_tick = await self.bridge.get_tick(req.symbol)
                if live_tick is None:
                    logger.error(f"No tick for {req.symbol}; re-queuing order")
                    await asyncio.sleep(0.5)
                    await self.execution_queue.put(req)
                    self.execution_queue.task_done()
                    continue
                live_bid = getattr(live_tick, "bid", 0.0)
                live_ask = getattr(live_tick, "ask", 0.0)
                spec = await self.bridge.get_symbol_spec(req.symbol)
                tick_size = getattr(spec, "trade_tick_size", 0.01) if spec else 0.01
                result = await self.bridge.place_pending_order(
                    symbol=req.symbol, direction=req.direction, volume=req.volume,
                    entry_price=req.price, bid=live_bid, ask=live_ask,
                    sl=req.sl, tp=req.tp, tick_size=tick_size,
                )
                
                with self.SessionLocal() as session:
                    idea = session.query(TradeIdea).get(req.idea_id)
                    attempt = session.query(TradeAttempt).filter_by(
                        trade_idea_id=idea.id, attempt_number=req.attempt_number
                    ).first()
                    
                    if result and result.retcode == 10009: # TRADE_RETCODE_DONE
                        attempt.execution_state = ExecutionState.SUBMITTED.value
                        attempt.mt5_ticket = getattr(result, 'order', 0)
                        
                        idea.state = TradeState.PENDING_ORDER_PLACED.value
                        
                        self._log_event(session, idea.id, "PENDING_ORDER_PLACED", {
                            "ticket": attempt.mt5_ticket,
                            "entry_price": req.price,
                            "chop_sl": req.sl,
                        })
                    else:
                        retcode = getattr(result, "retcode", None) if result else None
                        if result is None:
                            attempt.execution_state = ExecutionState.CANCELLED.value
                            attempt.exit_reason = "PENDING_DEFERRED"
                            idea.state = TradeState.WAITING_FOR_SETUP.value
                            self._log_event(session, idea.id, "PENDING_DEFERRED", {
                                "entry": req.price,
                                "bid": live_bid,
                                "ask": live_ask,
                                "reason": "would_fill_immediately_or_bad_rest",
                            })
                            logger.info(
                                f"[{req.symbol}] Idea {idea.id} pending @ {req.price} deferred "
                                f"(bid={live_bid}, ask={live_ask}) — will retry when price allows"
                            )
                        else:
                            attempt.execution_state = ExecutionState.REJECTED.value
                            attempt.exit_reason = f"REJECTED: {retcode}"
                            attempt.closed_at = utcnow()
                            idea.state = TradeState.IDEA_INVALIDATED.value
                            self._log_event(session, idea.id, "ENTRY_REJECTED", {
                                "reason": attempt.exit_reason,
                            })
                    
                    idea.version += 1
                    session.commit()

                # If the pending order fills immediately, ensure chop SL is on the position
                if result and result.retcode == 10009:
                    order_ticket = getattr(result, 'order', 0)
                    chop_sl = req.sl
                    for _ in range(20):
                        positions = await self.bridge.get_positions(req.symbol)
                        pos_ticket = self._resolve_position_ticket(
                            positions or [], order_ticket,
                            direction=req.direction, volume=req.volume,
                            entry_price=req.price,
                        )
                        if pos_ticket is not None:
                            fill_contract = SymbolContract.for_symbol(req.symbol)
                            ok = await self._ensure_chop_stop_on_position(
                                req.symbol, pos_ticket, chop_sl, req.tp, fill_contract
                            )
                            if ok:
                                logger.info(
                                    f"[{req.symbol}] Immediate fill on order {order_ticket}; "
                                    f"chop SL {chop_sl} confirmed on position {pos_ticket}"
                                )
                            else:
                                logger.error(
                                    f"[{req.symbol}] Immediate fill on order {order_ticket} "
                                    f"but chop SL {chop_sl} could not be secured"
                                )
                            break
                        await asyncio.sleep(0.05)
                
                self.execution_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Execution loop error: {e}")

    async def symbol_worker(self, symbol: str):
        logger.info(f"Worker active for {symbol}")
        state = self.symbol_states[symbol]
        
        while self.running:
            try:
                # Always fetch a fresh tick — do not gate the whole loop on price_feed state
                tick = await self.bridge.get_tick(symbol)
                if tick is None:
                    await asyncio.sleep(1.0)
                    continue
                market = MarketTick.from_mt5(tick)
                if not market.is_valid():
                    await asyncio.sleep(1.0)
                    continue
                state.latest_bid = market.bid
                state.latest_ask = market.ask
                state.last_tick_time = time.monotonic()
                
                with self.SessionLocal() as session:
                    ideas = session.query(TradeIdea).filter(
                        TradeIdea.symbol == symbol,
                        TradeIdea.state.in_([
                            TradeState.WAITING_FOR_SETUP.value,
                            TradeState.PENDING_ORDER_PLACED.value,
                            TradeState.WAITING_FOR_REENTRY.value,
                            TradeState.TRADE_OPEN.value,
                            "SUBMITTING_ORDER",
                        ])
                    ).all()
                    
                    for idea in ideas:
                        if idea.expires_at and utcnow() > as_utc(idea.expires_at):
                            idea.state = TradeState.IDEA_EXPIRED.value
                            self._log_event(session, idea.id, "EXPIRED")
                            idea.version += 1
                            session.commit()
                            continue

                        # Final SL/TP breach: cancel any live untriggered MT5 order, then kill idea
                        if idea.state in self._PRE_ENTRY_STATES:
                            pre_tick = await self.bridge.get_tick(symbol)
                            if pre_tick is None:
                                continue
                            pre_bid = getattr(pre_tick, 'bid', 0.0)
                            pre_ask = getattr(pre_tick, 'ask', 0.0)
                            if pre_bid == 0.0 or pre_ask == 0.0:
                                continue
                            sl_price = self._pre_entry_sl_price(idea, pre_bid, pre_ask)
                            tp_price = self._pre_entry_tp_price(idea, pre_bid, pre_ask)
                            sl_hit_pre = self._final_hard_sl_breached(idea, sl_price)
                            tp_hit_pre = self._take_profit_breached_before_entry(idea, tp_price)
                            if sl_hit_pre or tp_hit_pre:
                                await self._cancel_pending_and_invalidate_pre_entry(
                                    session, idea,
                                    sl_price if sl_hit_pre else tp_price,
                                    sl_hit_pre,
                                )
                                continue

                        if idea.state in [TradeState.WAITING_FOR_SETUP.value, TradeState.WAITING_FOR_REENTRY.value]:
                            # Place limit/stop in MT5 immediately so MT5 fills on the way back to entry

                            open_positions = await self.bridge.get_positions(symbol)
                            if open_positions is None:
                                continue
                            if await self._recover_or_block_reentry(
                                session, idea, symbol, open_positions
                            ):
                                continue
                            
                            if not self.session_manager.is_in_prime_session(symbol):
                                continue # Wait for prime session to place the order
                                
                            spec = await self.bridge.get_symbol_spec(symbol)
                            if spec is None:
                                logger.warning(f"Could not get symbol spec for {symbol}. MT5 disconnected? Skipping tick.")
                                continue

                            contract = SymbolContract.from_mt5_spec(symbol, spec)

                            account_info = await self.bridge.get_account_info()
                            account_equity = getattr(account_info, 'equity', None)
                            
                            remaining_risk = idea.max_idea_risk - idea.consumed_risk

                            if not self.risk_manager.can_accept_idea(
                                session, symbol, idea.direction, idea.original_hard_stop,
                                idea.original_entry, remaining_risk, account_equity,
                                exclude_idea_id=idea.id
                            ):
                                logger.warning(
                                    f"[{symbol}] Idea {idea.id} blocked by portfolio risk "
                                    f"(remaining_risk={remaining_risk:.2f}, equity={account_equity})"
                                )
                                continue

                            tick = await self.bridge.get_tick(symbol)
                            if tick is None:
                                continue
                            market = MarketTick.from_mt5(tick)
                            if not market.is_valid():
                                continue
                            
                            volume = contract.lot_size_for_risk(
                                remaining_risk, idea.original_entry, idea.original_hard_stop
                            )
                            idea.lot_size = volume
                            
                            chop_sl = self.config.chop_exit_price(
                                symbol, idea.original_entry, idea.direction
                            )

                            entry_plan = self.bridge.plan_pending_entry(
                                idea.direction, idea.original_entry,
                                market.bid, market.ask,
                                tick_size=contract.tick_size,
                            )
                            if entry_plan.would_fill_immediately:
                                logger.info(
                                    f"[{symbol}] Idea {idea.id} entry {idea.original_entry} "
                                    f"deferred — {entry_plan.defer_reason}"
                                )
                                self._log_event(session, idea.id, "PENDING_DEFERRED", {
                                    "entry": idea.original_entry,
                                    "bid": market.bid,
                                    "ask": market.ask,
                                    "kind": entry_plan.kind.value,
                                    "reason": entry_plan.defer_reason,
                                })
                                idea.version += 1
                                session.commit()
                                continue
                            
                            attempt_num = idea.retries_used + 1
                            attempt = TradeAttempt(
                                trade_idea_id=idea.id,
                                attempt_number=attempt_num,
                                execution_state=ExecutionState.PENDING.value,
                                entry_price=idea.original_entry,
                                volume=volume
                            )
                            session.add(attempt)
                            
                            req = ExecutionRequest(
                                idea_id=idea.id, symbol=idea.symbol, direction=idea.direction,
                                volume=volume, price=idea.original_entry,
                                sl=chop_sl, tp=idea.take_profit,
                                attempt_number=attempt_num,
                                current_price=market.bid,
                                bid=market.bid, ask=market.ask,
                            )
                            await self.execution_queue.put(req)
                            
                            idea.state = "SUBMITTING_ORDER" # Temporary state so worker ignores it until executor acts
                            idea.version += 1
                            session.commit()
                            continue

                        elif idea.state == TradeState.PENDING_ORDER_PLACED.value:
                            attempt = session.query(TradeAttempt).filter_by(
                                trade_idea_id=idea.id, execution_state=ExecutionState.SUBMITTED.value
                            ).order_by(TradeAttempt.id.desc()).first()
                            
                            if not attempt or not attempt.mt5_ticket:
                                continue # Still submitting or ticket not yet written by executor
                                
                            orders = await self.bridge.get_orders(symbol)
                            positions = await self.bridge.get_positions(symbol)
                            
                            if orders is None or positions is None:
                                continue # MT5 disconnected or error fetching

                            pending_spec = await self.bridge.get_symbol_spec(symbol)
                            pending_contract = (
                                SymbolContract.from_mt5_spec(symbol, pending_spec)
                                if pending_spec else SymbolContract.for_symbol(symbol)
                            )

                            tick = await self.bridge.get_tick(symbol)
                            if tick is None:
                                continue
                            market = MarketTick.from_mt5(tick)
                            live_bid, live_ask = market.bid, market.ask

                            expected_type = self.bridge.expected_pending_order_type(
                                idea.direction, idea.original_entry, live_bid, live_ask,
                                tick_size=pending_contract.tick_size,
                            )
                            for o in orders:
                                if getattr(o, 'ticket', 0) != attempt.mt5_ticket:
                                    continue
                                if expected_type is not None and getattr(o, 'type', None) != expected_type:
                                    chop_sl = self.config.chop_exit_price(
                                        symbol, attempt.entry_price, idea.direction
                                    )
                                    cancel_res = await self.bridge.cancel_pending_order(
                                        attempt.mt5_ticket
                                    )
                                    if cancel_res is None:
                                        logger.warning(
                                            f"[{symbol}] Failed to cancel stale pending "
                                            f"{attempt.mt5_ticket} for type replacement"
                                        )
                                        break
                                    replace_res = await self.bridge.place_pending_order(
                                        symbol=symbol, direction=idea.direction,
                                        volume=attempt.volume, entry_price=attempt.entry_price,
                                        bid=live_bid, ask=live_ask,
                                        sl=chop_sl, tp=idea.take_profit,
                                        tick_size=pending_contract.tick_size,
                                    )
                                    if replace_res and replace_res.retcode == 10009:
                                        attempt.mt5_ticket = getattr(replace_res, 'order', 0)
                                        self._log_event(session, idea.id, "ORDER_TYPE_REPLACED", {
                                            "old_type": getattr(o, 'type', None),
                                            "new_type": expected_type,
                                            "ticket": attempt.mt5_ticket,
                                            "bid": live_bid,
                                            "ask": live_ask,
                                        })
                                        idea.version += 1
                                        session.commit()
                                        logger.info(
                                            f"[{symbol}] Replaced stale pending order — "
                                            f"now type {expected_type} @ {attempt.entry_price} "
                                            f"(ask={live_ask})"
                                        )
                                    else:
                                        attempt.execution_state = ExecutionState.CANCELLED.value
                                        attempt.exit_reason = "ORDER_TYPE_REPLACE_FAILED"
                                        idea.state = TradeState.WAITING_FOR_REENTRY.value
                                        idea.version += 1
                                        session.commit()
                                    break
                                
                            # Check if the pending order has converted to a position
                            pos_ticket = None
                            for _ in range(30):
                                positions = await self.bridge.get_positions(symbol)
                                if positions is None:
                                    break
                                pos_ticket = self._resolve_position_ticket(
                                    positions, attempt.mt5_ticket,
                                    direction=idea.direction, volume=attempt.volume,
                                    entry_price=attempt.entry_price,
                                )
                                if pos_ticket is not None:
                                    break
                                await asyncio.sleep(0.05)
                            if pos_ticket is not None:
                                chop_sl = self.config.chop_exit_price(
                                    symbol, attempt.entry_price, idea.direction
                                )
                                idea.hard_stop = chop_sl

                                attempt.execution_state = ExecutionState.FILLED.value
                                idea.state = TradeState.TRADE_OPEN.value

                                op = OpenPosition(
                                    trade_idea_id=idea.id,
                                    mt5_ticket=pos_ticket,
                                    symbol=symbol,
                                    direction=idea.direction,
                                    volume=attempt.volume,
                                    entry_price=attempt.entry_price,
                                    current_stop=chop_sl,
                                    current_tp=idea.take_profit
                                )
                                session.add(op)
                                self._log_event(session, idea.id, "ENTRY", {
                                    "ticket": pos_ticket,
                                    "order_ticket": attempt.mt5_ticket,
                                    "chop_sl": chop_sl,
                                })
                                idea.version += 1
                                session.commit()

                                if await self._ensure_chop_stop_on_position(
                                    symbol, pos_ticket, chop_sl, idea.take_profit,
                                    pending_contract,
                                ):
                                    self._log_event(session, idea.id, "CHOP_STOP_SET", {
                                        "chop_sl": chop_sl,
                                        "distance": self.config.chop_distance_for(symbol),
                                    })
                                    session.commit()
                                else:
                                    logger.warning(
                                        f"[{symbol}] Failed to secure chop SL {chop_sl} on "
                                        f"position {pos_ticket}"
                                    )
                                continue
                                
                            # Check if the pending order is still active
                            still_pending = any(getattr(o, 'ticket', 0) == attempt.mt5_ticket for o in orders)
                            if not still_pending:
                                # Not filled and not pending -> Cancelled/Rejected by MT5
                                attempt.execution_state = ExecutionState.CANCELLED.value
                                attempt.exit_reason = "MT5_CANCELLED"
                                idea.state = TradeState.IDEA_INVALIDATED.value
                                self._log_event(session, idea.id, "IDEA_INVALIDATED", {"reason": "ORDER_CANCELLED_BY_MT5"})
                                idea.version += 1
                                session.commit()
                                continue
                                
                        elif idea.state == TradeState.TRADE_OPEN.value:
                            attempt = session.query(TradeAttempt).filter_by(
                                trade_idea_id=idea.id, execution_state=ExecutionState.FILLED.value
                            ).order_by(TradeAttempt.id.desc()).first()
                            
                            open_pos = session.query(OpenPosition).filter_by(trade_idea_id=idea.id).first()
                            
                            if not attempt or not open_pos:
                                continue

                            tick = await self.bridge.get_tick(symbol)
                            if tick is None:
                                continue
                            live_bid = getattr(tick, 'bid', 0.0)
                            live_ask = getattr(tick, 'ask', 0.0)
                            if live_bid == 0.0 or live_ask == 0.0:
                                continue

                            spec = await self.bridge.get_symbol_spec(symbol)
                            if spec is None:
                                continue

                            contract = SymbolContract.from_mt5_spec(symbol, spec)

                            actual_entry = attempt.entry_price
                            chop_sl = self.config.chop_exit_price(
                                symbol, actual_entry, idea.direction
                            )
                            market = MarketTick(live_bid, live_ask)
                            mark_price = market.open_trade_sl_price(idea.direction)

                            mt5_positions = await self.bridge.get_positions(symbol)
                            if mt5_positions is not None:
                                still_open = any(
                                    getattr(p, 'ticket', 0) == open_pos.mt5_ticket
                                    for p in mt5_positions
                                )
                                if not still_open:
                                    close_price = await self.bridge.get_position_close_price(
                                        open_pos.mt5_ticket
                                    )
                                    if close_price is None:
                                        close_price = mark_price
                                    await self._handle_trade_closed(
                                        session, idea, attempt, open_pos, symbol,
                                        close_price, chop_sl, exit_reason="MT5_STOP",
                                        contract=contract,
                                        hard_stop_at_close=open_pos.current_stop,
                                    )
                                    continue

                            # --- Time Based Exit (8H Max Hold) ---
                            elapsed_time = utcnow() - as_utc(attempt.opened_at)
                            time_hit = elapsed_time >= timedelta(hours=8)

                            stop_hit = TradeLevels.working_stop_breached(
                                idea.direction, idea.hard_stop, mark_price
                            )

                            tp_price = market.open_trade_tp_price(idea.direction)
                            tp_hit = TradeLevels.take_profit_breached(
                                idea.direction, idea.take_profit, tp_price
                            )

                            if stop_hit or tp_hit or time_hit:
                                close_res = await self.bridge.close_position(
                                    ticket=open_pos.mt5_ticket, symbol=symbol,
                                    direction=idea.direction, volume=open_pos.volume
                                )
                                close_price = getattr(close_res, 'price', mark_price) if close_res else mark_price

                                if time_hit:
                                    exit_reason = "MAX_HOLD_EXCEEDED"
                                elif tp_hit:
                                    exit_reason = "TAKE_PROFIT"
                                elif idea.direction == "BUY" and idea.hard_stop > chop_sl:
                                    exit_reason = "TRAILING_STOP"
                                elif idea.direction == "SELL" and idea.hard_stop < chop_sl:
                                    exit_reason = "TRAILING_STOP"
                                else:
                                    exit_reason = "CHOP_EXIT"

                                await self._handle_trade_closed(
                                    session, idea, attempt, open_pos, symbol,
                                    close_price, chop_sl, exit_reason=exit_reason,
                                    contract=contract,
                                    hard_stop_at_close=idea.hard_stop,
                                    elapsed_time=elapsed_time if time_hit else None,
                                )
                                continue
                            
                            else:
                                new_sl = self.trailing_engine.calculate_new_stop(
                                    direction=idea.direction, attempt_entry=actual_entry,
                                    current_price=mark_price, current_hard_stop=idea.hard_stop,
                                    consumed_risk=idea.consumed_risk, take_profit=idea.take_profit,
                                    lot_size=open_pos.volume,
                                    tick_value=contract.tick_value,
                                    tick_size=contract.tick_size,
                                    idea_realized_pnl=idea.realized_pnl,
                                    min_move_distance=contract.price_tolerance(multiplier=5.0),
                                    symbol=contract.symbol,
                                )
                                if new_sl is not None:
                                    mod_res = await self.bridge.modify_position(
                                        ticket=open_pos.mt5_ticket, symbol=symbol,
                                        sl=new_sl, tp=idea.take_profit
                                    )
                                    if mod_res:
                                        idea.hard_stop = new_sl
                                        open_pos.current_stop = new_sl
                                        idea.version += 1
                                        self._log_event(session, idea.id, "TRAILING_UPDATE", {"new_sl": new_sl})
                                        session.commit()
                                        logger.info(f"[{symbol}] Trailing Stop Adjusted to {new_sl}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"{symbol} Worker error: {e}", exc_info=True)
                await asyncio.sleep(5.0)
                continue
                
            await asyncio.sleep(0.1)

if __name__ == "__main__":
    manager = TradeManager()
    try:
        asyncio.run(manager.start())
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Shutting down...")
        asyncio.run(manager.stop())
