import asyncio
import logging
import json
import time
from typing import Dict
from datetime import timedelta

from src.core.models import (
    TradeIdea, TradeState, SymbolState, ExecutionRequest,
    TradeAttempt, TradeEvent, OpenPosition, ExecutionState, utcnow
)
from src.core.database import init_db, get_session_local
from src.execution.bridge import MT5Bridge, ConnectionState
from src.execution.trailing import TrailingStopEngine
from src.risk.quant_filters import SessionManager, LiquidityFilter
from src.risk.portfolio import PortfolioRiskManager, PositionSizingEngine

logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                    handlers=[logging.FileHandler("trade_manager.log"), logging.StreamHandler()])
logger = logging.getLogger(__name__)

class TradeManager:
    def __init__(self, db_url="sqlite:///trade_ideas.db"):
        self.engine = init_db(db_url)
        self.SessionLocal = get_session_local(self.engine)
        self.running = False
        
        self.symbol_states: Dict[str, SymbolState] = {}
        self.execution_queue = asyncio.Queue()
        
        self.bridge = MT5Bridge()
        self.session_manager = SessionManager()
        self.liquidity_filter = LiquidityFilter()
        self.trailing_engine = TrailingStopEngine(progressive_secure_percentage=0.6)
        self.risk_manager = PortfolioRiskManager()
        
        self.tasks = []
        
    def _log_event(self, session, idea_id: int, event_type: str, data: dict = None):
        event = TradeEvent(
            trade_idea_id=idea_id,
            event_type=event_type,
            event_data=json.dumps(data) if data else None
        )
        session.add(event)

    async def start(self):
        self.running = True
        logger.info("Starting Modular Trade Manager...")
        
        if not await self.bridge.connect():
            logger.warning("Failed to connect to MT5. Will attempt reconnection in loop.")
            
        await self._reconcile_positions_on_startup()
        
        self.tasks.append(asyncio.create_task(self.price_feed_loop()))
        self.tasks.append(asyncio.create_task(self.order_executor_loop()))
        self.tasks.append(asyncio.create_task(self.worker_registration_loop()))
        
        while self.running:
            if self.bridge.connection_state == ConnectionState.DISCONNECTED:
                await self.bridge.reconnect()
            await asyncio.sleep(5.0)

    async def stop(self):
        self.running = False
        for task in self.tasks:
            task.cancel()
        await self.bridge.shutdown()
        logger.info("Trade Manager stopping.")

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
                result = await self.bridge.place_pending_order(
                    symbol=req.symbol, direction=req.direction, volume=req.volume,
                    entry_price=req.price, current_price=req.current_price, sl=req.sl, tp=req.tp
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
                        
                        self._log_event(session, idea.id, "PENDING_ORDER_PLACED", {"ticket": attempt.mt5_ticket, "entry_price": req.price})
                    else:
                        attempt.execution_state = ExecutionState.REJECTED.value
                        attempt.exit_reason = f"REJECTED: {getattr(result, 'retcode', 'None')}" if result else "BRIDGE_ERROR"
                        attempt.closed_at = utcnow()
                        
                        idea.state = TradeState.IDEA_INVALIDATED.value
                        self._log_event(session, idea.id, "ENTRY_REJECTED", {"reason": attempt.exit_reason})
                    
                    idea.version += 1
                    session.commit()
                
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
                current_price = state.latest_bid
                if current_price == 0.0:
                    await asyncio.sleep(1.0)
                    continue
                
                with self.SessionLocal() as session:
                    ideas = session.query(TradeIdea).filter(
                        TradeIdea.symbol == symbol,
                        TradeIdea.state.in_([
                            TradeState.WAITING_FOR_SETUP.value,
                            TradeState.PENDING_ORDER_PLACED.value,
                            TradeState.WAITING_FOR_REENTRY.value,
                            TradeState.TRADE_OPEN.value
                        ])
                    ).all()
                    
                    for idea in ideas:
                        if idea.expires_at and utcnow() > idea.expires_at:
                            idea.state = TradeState.IDEA_EXPIRED.value
                            self._log_event(session, idea.id, "EXPIRED")
                            idea.version += 1
                            session.commit()
                            continue
                            
                        if idea.state in [TradeState.WAITING_FOR_SETUP.value, TradeState.WAITING_FOR_REENTRY.value]:
                            # Instead of waiting for price to reach the entry zone, we immediately
                            # calculate the lot size and queue a pending order to MT5.
                            
                            if not self.session_manager.is_in_prime_session(symbol):
                                continue # Wait for prime session to place the order
                                
                            spec = await self.bridge.get_symbol_spec(symbol)
                            if spec is None:
                                logger.warning(f"Could not get symbol spec for {symbol}. MT5 disconnected? Skipping tick.")
                                continue

                            tick_value = getattr(spec, 'trade_tick_value', 1.0)
                            tick_size = getattr(spec, 'trade_tick_size', 0.00001)
                            lot_step = getattr(spec, 'volume_step', 0.01)
                            lot_min = getattr(spec, 'volume_min', 0.01)
                            lot_max = getattr(spec, 'volume_max', 100.0)

                            account_info = await self.bridge.get_account_info()
                            account_equity = getattr(account_info, 'equity', None)
                            
                            remaining_risk = idea.max_idea_risk - idea.consumed_risk

                            if not self.risk_manager.can_accept_idea(
                                session, symbol, idea.direction, idea.hard_stop, 
                                idea.original_entry, remaining_risk, account_equity
                            ):
                                continue
                            
                            volume = PositionSizingEngine.calculate_lot_size(
                                risk_amount=remaining_risk, entry_price=idea.original_entry, 
                                stop_loss=idea.hard_stop, tick_value=tick_value, 
                                tick_size=tick_size, lot_step=lot_step, lot_min=lot_min, lot_max=lot_max
                            )
                            idea.lot_size = volume
                            
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
                                volume=volume, price=idea.original_entry, sl=idea.hard_stop, tp=idea.take_profit,
                                attempt_number=attempt_num, current_price=current_price
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
                                
                            # Check if the pending order has converted to a position
                            filled = any(getattr(p, 'identifier', 0) == attempt.mt5_ticket for p in positions)
                            if filled:
                                attempt.execution_state = ExecutionState.FILLED.value
                                idea.state = TradeState.TRADE_OPEN.value
                                
                                op = OpenPosition(
                                    trade_idea_id=idea.id,
                                    mt5_ticket=attempt.mt5_ticket,
                                    symbol=symbol,
                                    direction=idea.direction,
                                    volume=attempt.volume,
                                    entry_price=attempt.entry_price,
                                    current_stop=idea.hard_stop,
                                    current_tp=idea.take_profit
                                )
                                session.add(op)
                                self._log_event(session, idea.id, "ENTRY", {"ticket": attempt.mt5_ticket})
                                idea.version += 1
                                session.commit()
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
                                
                            # Still pending -> Check if live price breached SL or TP prematurely
                            sl_hit_pre = (idea.direction == "BUY" and current_price <= idea.original_hard_stop) or \
                                         (idea.direction == "SELL" and current_price >= idea.original_hard_stop)
                            
                            tp_hit_pre = (idea.direction == "BUY" and current_price >= idea.take_profit) or \
                                         (idea.direction == "SELL" and current_price <= idea.take_profit)
                                         
                            if sl_hit_pre or tp_hit_pre:
                                cancel_res = await self.bridge.cancel_pending_order(attempt.mt5_ticket)
                                if cancel_res is None:
                                    logger.warning(f"Failed to cancel pending order {attempt.mt5_ticket}. Will retry next tick.")
                                    continue
                                
                                attempt.execution_state = ExecutionState.CANCELLED.value
                                attempt.exit_reason = "PRE_ENTRY_BREACH"
                                idea.state = TradeState.IDEA_INVALIDATED.value
                                reason = "SL_HIT_BEFORE_ENTRY" if sl_hit_pre else "TP_HIT_BEFORE_ENTRY"
                                self._log_event(session, idea.id, "PRE_ENTRY_INVALIDATED", {"reason": reason, "price": current_price})
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
                            
                            actual_entry = attempt.entry_price
                            
                            # --- Time Based Exit (8H Max Hold) ---
                            elapsed_time = utcnow() - attempt.opened_at
                            time_hit = elapsed_time >= timedelta(hours=8)
                            
                            stop_hit = (idea.direction == "BUY" and current_price <= idea.hard_stop) or \
                                       (idea.direction == "SELL" and current_price >= idea.hard_stop)
                            
                            tp_hit = (idea.direction == "BUY" and current_price >= idea.take_profit) or \
                                     (idea.direction == "SELL" and current_price <= idea.take_profit)

                            if stop_hit or tp_hit or time_hit:
                                close_res = await self.bridge.close_position(
                                    ticket=open_pos.mt5_ticket, symbol=symbol, 
                                    direction=idea.direction, volume=open_pos.volume
                                )
                                
                                close_price = getattr(close_res, 'price', current_price) if close_res else current_price
                                pnl = (close_price - actual_entry) if idea.direction == "BUY" else (actual_entry - close_price)
                                
                                attempt.exit_price = close_price
                                attempt.pnl = pnl
                                attempt.closed_at = utcnow()
                                
                                idea.realized_pnl += pnl
                                session.delete(open_pos)
                                
                                if time_hit:
                                    attempt.exit_reason = "MAX_HOLD_EXCEEDED"
                                    idea.state = TradeState.IDEA_INVALIDATED.value
                                    self._log_event(session, idea.id, "TIME_EXIT", {"pnl": pnl, "elapsed_hours": elapsed_time.total_seconds() / 3600})
                                elif stop_hit:
                                    if pnl < 0:
                                        idea.consumed_risk += abs(pnl)
                                        
                                    attempt.exit_reason = "STOP_LOSS"
                                    idea.retries_used += 1
                                    self._log_event(session, idea.id, "EARLY_EXIT", {"pnl": pnl, "consumed_risk": idea.consumed_risk})
                                    
                                    # Permanent Kill only when total losses exceed the max allowed risk budget
                                    if idea.consumed_risk >= idea.max_idea_risk or idea.retries_used >= idea.max_retries:
                                        idea.state = TradeState.IDEA_INVALIDATED.value
                                        reason = "RISK_EXHAUSTED" if idea.consumed_risk >= idea.max_idea_risk else "MAX_RETRIES"
                                        self._log_event(session, idea.id, "IDEA_INVALIDATED", {"reason": reason, "consumed_risk": idea.consumed_risk})
                                    else:
                                        # Still have risk budget left — wait for re-entry
                                        idea.state = TradeState.WAITING_FOR_REENTRY.value
                                        idea.hard_stop = idea.original_hard_stop  # Reset stop to original
                                        self._log_event(session, idea.id, "WAITING_FOR_REENTRY", {"remaining_risk": idea.max_idea_risk - idea.consumed_risk})
                                else:
                                    attempt.exit_reason = "TAKE_PROFIT"
                                    idea.state = TradeState.TP_REACHED.value
                                    self._log_event(session, idea.id, "TP_REACHED", {"pnl": pnl})
                                
                                idea.version += 1
                                session.commit()
                            
                            else:
                                # Fetch contract specs for Dollar-to-Price conversion
                                spec = await self.bridge.get_symbol_spec(symbol)
                                if spec is None:
                                    continue  # MT5 disconnected, skip trailing this tick

                                t_tick_value = getattr(spec, 'trade_tick_value', 1.0)
                                t_tick_size = getattr(spec, 'trade_tick_size', 0.00001)

                                new_sl = self.trailing_engine.calculate_new_stop(
                                    direction=idea.direction, attempt_entry=actual_entry,
                                    current_price=current_price, current_hard_stop=idea.hard_stop,
                                    consumed_risk=idea.consumed_risk, take_profit=idea.take_profit,
                                    lot_size=open_pos.volume, tick_value=t_tick_value,
                                    tick_size=t_tick_size, min_move_distance=t_tick_size * 5
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
