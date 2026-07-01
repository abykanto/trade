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
from sqlalchemy import func, or_

from src.core.database import init_db, get_session_local, as_utc
from src.core.config import load_trading_config, TradingConfig
from src.execution.factory import create_execution_bridge
from src.execution.bridge import ConnectionState
from src.execution.ea_bridge import EABridge
from src.execution.trailing import TrailingStopEngine
from src.risk.quant_filters import SessionManager, LiquidityFilter
from src.risk.portfolio import PortfolioRiskManager, PositionSizingEngine
from src.market.position import (
    find_idea_position,
    position_direction,
    resolve_position_ticket,
)
from src.market.contract import SymbolContract
from src.market.tick import MarketTick
from src.market.levels import TradeLevels
from src.market.pending_entry import entry_market_side, ready_for_initial_entry_placement
from src.market.deal_history import order_matches_attempt
from src.market.bot_orders import is_bot_placed_order
from src.market.order_outcome import PendingOrderOutcome
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
        
        self.bridge = create_execution_bridge()
        self.session_manager = SessionManager()
        self.liquidity_filter = LiquidityFilter()
        self.trailing_engine = TrailingStopEngine(progressive_secure_percentage=0.6)
        self.risk_manager = PortfolioRiskManager(
            daily_dd_pct=float(os.environ.get("DAILY_DD_PCT", "0.03")),
            max_account_risk_percent=float(os.environ.get("MAX_ACCOUNT_RISK_PCT", "6.0")),
        )
        self.price_parquet_logger = XauusdPriceParquetLogger(
            flush_every_rows=int(os.environ.get("PRICE_LOG_FLUSH_ROWS", "30")),
        )
        
        self.tasks = []
        self._entry_zone_defer_logged: set[int] = set()
        self._ea_symbol_wake: set[str] = set()
        self._ea_force_resolve_symbols: set[str] = set()
        self._bot_cancelled_tickets: set[int] = set()
        
    def _log_event(self, session, idea_id: int, event_type: str, data: dict = None):
        event = TradeEvent(
            trade_idea_id=idea_id,
            event_type=event_type,
            event_data=json.dumps(data) if data else None
        )
        session.add(event)

    async def _cancel_pending_order_as_bot(self, ticket: int):
        """Record a bot-initiated cancel so external deletes can be distinguished."""
        if ticket:
            self._bot_cancelled_tickets.add(int(ticket))
        return await self.bridge.cancel_pending_order(ticket)

    def _is_external_order_cancel(
        self, ticket: int, outcome: PendingOrderOutcome
    ) -> bool:
        """True when MT5 shows CANCELLED but this process did not request it."""
        ticket = int(ticket or 0)
        if ticket in self._bot_cancelled_tickets:
            return False
        if not is_bot_placed_order(
            magic=int(outcome.order_magic or 0),
            bot_magic=self.bridge.magic,
            comment=outcome.order_comment,
        ):
            return True
        return True

    async def _invalidate_external_order_cancel(
        self,
        session,
        idea: TradeIdea,
        attempt: TradeAttempt,
        outcome: PendingOrderOutcome,
    ) -> None:
        attempt.execution_state = ExecutionState.CANCELLED.value
        attempt.exit_reason = "EXTERNAL_CANCEL"
        idea.state = TradeState.IDEA_INVALIDATED.value
        self._log_event(session, idea.id, "EXTERNAL_ORDER_CANCELLED", {
            "ticket": attempt.mt5_ticket,
            "comment": outcome.order_comment,
            "magic": outcome.order_magic,
        })
        idea.version += 1
        session.commit()
        logger.info(
            f"[{idea.symbol}] Idea {idea.id} invalidated: pending order "
            f"{attempt.mt5_ticket} cancelled externally in MT5"
        )

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
            if close_res is None:
                logger.error(
                    f"[{symbol}] Emergency close failed for position {pos_ticket} "
                    f"after chop SL could not be set"
                )
            return False
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
            pos_ticket = find_idea_position(
                positions,
                idea.direction,
                self.bridge.magic,
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
        attempt.opened_at = utcnow()
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

    async def _try_recover_open_position(
        self,
        session,
        idea: TradeIdea,
        symbol: str,
        attempt: TradeAttempt | None,
        contract: SymbolContract,
    ) -> bool:
        """If MT5 has a live position for this idea, register TRADE_OPEN."""
        positions = await self.bridge.get_positions(symbol)
        if not positions:
            return False
        if attempt is None:
            attempt = session.query(TradeAttempt).filter_by(
                trade_idea_id=idea.id,
            ).order_by(TradeAttempt.id.desc()).first()
        if attempt is None:
            return False

        pos_ticket = self._resolve_position_ticket(
            positions,
            attempt.mt5_ticket or 0,
            direction=idea.direction,
            volume=attempt.volume,
            entry_price=attempt.entry_price,
        )
        if pos_ticket is None:
            pos_ticket = find_idea_position(
                positions,
                idea.direction,
                self.bridge.magic,
                volume=attempt.volume,
                entry_price=attempt.entry_price,
            )
        if pos_ticket is None:
            return False

        await self._register_trade_open(
            session, idea, attempt, symbol, pos_ticket, contract
        )
        self._log_event(session, idea.id, "POSITION_RECOVERED", {
            "ticket": pos_ticket,
            "order_ticket": attempt.mt5_ticket,
            "source": "live_position_check",
        })
        session.commit()
        logger.info(
            f"[{symbol}] Recovered open position {pos_ticket} for idea {idea.id} "
            f"before re-entry"
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
            cancel_res = await self._cancel_pending_order_as_bot(attempt.mt5_ticket)
            if cancel_res is None:
                spec = await self.bridge.get_symbol_spec(idea.symbol)
                contract = (
                    SymbolContract.from_mt5_spec(idea.symbol, spec)
                    if spec else SymbolContract.for_symbol(idea.symbol)
                )
                outcome = await self.bridge.resolve_pending_order_outcome(
                    symbol=idea.symbol,
                    order_ticket=attempt.mt5_ticket,
                    direction=idea.direction,
                    volume=attempt.volume,
                    entry_price=attempt.entry_price,
                    tick_size=contract.tick_size,
                )
                if outcome.status == "pending":
                    logger.warning(
                        f"[{idea.symbol}] Failed to cancel pending order {attempt.mt5_ticket} "
                        f"for idea {idea.id} — will retry next tick."
                    )
                    return False
                if outcome.status == "open" and outcome.position_ticket:
                    await self._register_trade_open(
                        session, idea, attempt, idea.symbol,
                        outcome.position_ticket, contract,
                    )
                    logger.info(
                        f"[{idea.symbol}] Pre-entry breach check recovered position "
                        f"{outcome.position_ticket} for idea {idea.id}"
                    )
                    return True
                if outcome.status == "closed":
                    if await self._handle_pending_order_outcome(
                        session, idea, attempt, idea.symbol, outcome, contract
                    ):
                        return True
                elif outcome.status == "missing":
                    if await self._try_recover_open_position(
                        session, idea, idea.symbol, attempt, contract
                    ):
                        return True
                attempt.execution_state = ExecutionState.CANCELLED.value
                attempt.exit_reason = "PRE_ENTRY_BREACH"
                logger.info(
                    f"[{idea.symbol}] Pending {attempt.mt5_ticket} no longer resting "
                    f"— invalidating idea {idea.id} past final SL {idea.original_hard_stop}"
                )
            else:
                attempt.execution_state = ExecutionState.CANCELLED.value
                attempt.exit_reason = "PRE_ENTRY_BREACH"
                logger.info(
                    f"[{idea.symbol}] Cancelled untriggered MT5 order {attempt.mt5_ticket} "
                    f"— idea {idea.id} no longer valid past final SL {idea.original_hard_stop}"
                )

        self._invalidate_pre_entry_breach(session, idea, price, sl_breached)
        return True

    @staticmethod
    def _pre_entry_waiting_state(idea: TradeIdea) -> str:
        if idea.retries_used > 0:
            return TradeState.WAITING_FOR_REENTRY.value
        return TradeState.WAITING_FOR_SETUP.value

    async def _register_trade_open(
        self,
        session,
        idea: TradeIdea,
        attempt: TradeAttempt,
        symbol: str,
        pos_ticket: int,
        pending_contract: SymbolContract,
    ) -> None:
        chop_sl = self.config.chop_exit_price(
            symbol, attempt.entry_price, idea.direction
        )
        idea.hard_stop = chop_sl
        attempt.execution_state = ExecutionState.FILLED.value
        attempt.opened_at = utcnow()
        idea.state = TradeState.TRADE_OPEN.value

        existing = session.query(OpenPosition).filter_by(trade_idea_id=idea.id).first()
        if existing:
            existing.mt5_ticket = pos_ticket
            existing.symbol = symbol
            existing.direction = idea.direction
            existing.volume = attempt.volume
            existing.entry_price = attempt.entry_price
            existing.current_stop = chop_sl
            existing.current_tp = idea.take_profit
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
        self._log_event(session, idea.id, "ENTRY", {
            "ticket": pos_ticket,
            "order_ticket": attempt.mt5_ticket,
            "chop_sl": chop_sl,
            "entry_price": attempt.entry_price,
        })
        idea.version += 1
        session.commit()

        if await self._ensure_chop_stop_on_position(
            symbol, pos_ticket, chop_sl, idea.take_profit, pending_contract,
        ):
            self._log_event(session, idea.id, "CHOP_STOP_SET", {
                "chop_sl": chop_sl,
                "distance": self.config.chop_distance_for(symbol),
            })
            session.commit()
        else:
            logger.warning(
                f"[{symbol}] Failed to secure chop SL {chop_sl} on position {pos_ticket}"
            )

    async def _requeue_pre_entry(
        self,
        session,
        idea: TradeIdea,
        attempt: TradeAttempt,
        exit_reason: str,
        event_type: str,
        extra: dict | None = None,
    ) -> None:
        """Return idea to waiting state so the worker re-places a pending order."""
        attempt.execution_state = ExecutionState.CANCELLED.value
        attempt.exit_reason = exit_reason
        idea.state = self._pre_entry_waiting_state(idea)
        payload = {"reason": exit_reason, "entry": idea.original_entry}
        if extra:
            payload.update(extra)
        self._log_event(session, idea.id, event_type, payload)
        idea.version += 1
        session.commit()
        logger.info(
            f"[{idea.symbol}] Idea {idea.id} re-queued for pending @ "
            f"{idea.original_entry} ({exit_reason})"
        )

    @staticmethod
    def _next_attempt_number(session, idea_id: int) -> int:
        last = session.query(func.max(TradeAttempt.attempt_number)).filter_by(
            trade_idea_id=idea_id,
        ).scalar()
        return int(last or 0) + 1

    async def _requeue_pending_lost(
        self,
        session,
        idea: TradeIdea,
        attempt: TradeAttempt,
        symbol: str,
        contract: SymbolContract,
        exit_reason: str,
        extra: dict | None = None,
    ) -> bool:
        """Re-place pending after a vanished order unless a live position is found."""
        if await self._try_recover_open_position(session, idea, symbol, attempt, contract):
            return True
        positions = await self.bridge.get_positions(symbol)
        if positions:
            pos_ticket = find_idea_position(
                positions,
                idea.direction,
                self.bridge.magic,
                volume=attempt.volume,
                entry_price=attempt.entry_price,
            )
            if pos_ticket is not None:
                await self._register_trade_open(
                    session, idea, attempt, symbol, pos_ticket, contract,
                )
                return True
        await self._requeue_pre_entry(
            session, idea, attempt, exit_reason, "PENDING_LOST_REQUEUE", extra,
        )
        return True

    async def _cancel_mismatched_reentry_pending(
        self,
        session,
        idea: TradeIdea,
        symbol: str,
        entry_plan,
        tick_size: float,
    ) -> int:
        """Cancel resting pending orders at entry when price moved needs a different kind."""
        orders = await self.bridge.get_orders(symbol)
        if not orders:
            return 0

        expected_type = entry_plan.mt5_order_type
        eps = tick_size if tick_size > 0 else 0.00001
        cancelled = 0

        for order in orders:
            ticket = int(getattr(order, "ticket", 0) or 0)
            if not ticket:
                continue
            otype = getattr(order, "type", None)
            oprice = float(
                getattr(order, "price_open", 0)
                or getattr(order, "price", 0)
                or 0
            )
            if abs(oprice - idea.original_entry) > max(eps * 2, 1e-9):
                continue
            if otype == expected_type:
                continue

            cancel_res = await self._cancel_pending_order_as_bot(ticket)
            if cancel_res is None:
                logger.warning(
                    f"[{symbol}] Could not cancel mismatched pending {ticket} "
                    f"(type {otype}, need {entry_plan.kind.value})"
                )
                continue

            cancelled += 1
            logger.info(
                f"[{symbol}] Cancelled mismatched pending {ticket} for idea {idea.id}: "
                f"had MT5 type {otype}, need {entry_plan.kind.value} @ {idea.original_entry} "
                f"(bid={entry_plan.bid}, ask={entry_plan.ask}, "
                f"price {entry_market_side(idea.direction, idea.original_entry, entry_plan.bid, entry_plan.ask, tick_size)})"
            )
            db_attempt = session.query(TradeAttempt).filter_by(
                trade_idea_id=idea.id,
                mt5_ticket=ticket,
                execution_state=ExecutionState.SUBMITTED.value,
            ).first()
            if db_attempt:
                db_attempt.execution_state = ExecutionState.CANCELLED.value
                db_attempt.exit_reason = "ORDER_KIND_STALE"
                idea.state = self._pre_entry_waiting_state(idea)
                session.commit()

        return cancelled

    async def _arm_reentry_pending(
        self,
        session,
        idea: TradeIdea,
        symbol: str,
        contract: SymbolContract,
    ) -> None:
        """After chop loss, pick limit/stop from live price vs entry and clear stale pendings."""
        tick = await self.bridge.get_tick(symbol)
        if tick is None:
            return
        market = MarketTick.from_mt5(tick)
        if not market.is_valid():
            return

        entry_plan = self.bridge.plan_pending_entry(
            idea.direction,
            idea.original_entry,
            market.bid,
            market.ask,
            tick_size=contract.tick_size,
        )
        await self._cancel_mismatched_reentry_pending(
            session, idea, symbol, entry_plan, contract.tick_size
        )
        price_side = entry_market_side(
            idea.direction,
            idea.original_entry,
            market.bid,
            market.ask,
            contract.tick_size,
        )
        self._log_event(session, idea.id, "REENTRY_ARMED", {
            "entry": idea.original_entry,
            "bid": market.bid,
            "ask": market.ask,
            "order_kind": entry_plan.kind.value,
            "price_side": price_side,
            "remaining_risk": idea.max_idea_risk - idea.consumed_risk,
        })
        idea.version += 1
        session.commit()
        logger.info(
            f"[{symbol}] Idea {idea.id} re-entry armed: {entry_plan.kind.value} @ "
            f"{idea.original_entry} ({price_side}, bid={market.bid}, ask={market.ask})"
        )

    async def _handle_pending_order_outcome(
        self,
        session,
        idea: TradeIdea,
        attempt: TradeAttempt,
        symbol: str,
        outcome,
        pending_contract: SymbolContract,
    ) -> bool:
        """Apply resolved MT5 pending-order outcome. True = handled, continue worker."""
        if outcome.status == "pending":
            return False

        if outcome.status == "open" and outcome.position_ticket:
            await self._register_trade_open(
                session, idea, attempt, symbol, outcome.position_ticket, pending_contract
            )
            if outcome.fill_price is not None:
                logger.info(
                    f"[{symbol}] Idea {idea.id} filled @ {outcome.fill_price} "
                    f"(requested {attempt.entry_price})"
                )
            return True

        if outcome.status == "closed":
            chop_sl = self.config.chop_exit_price(
                symbol, attempt.entry_price, idea.direction
            )
            fill_price = outcome.fill_price or attempt.entry_price
            close_price = outcome.close_price or fill_price
            attempt.execution_state = ExecutionState.FILLED.value
            attempt.opened_at = utcnow()
            open_pos = OpenPosition(
                trade_idea_id=idea.id,
                mt5_ticket=outcome.position_ticket or 0,
                symbol=symbol,
                direction=idea.direction,
                volume=attempt.volume,
                entry_price=attempt.entry_price,
                current_stop=chop_sl,
                current_tp=idea.take_profit,
            )
            session.add(open_pos)
            idea.state = TradeState.TRADE_OPEN.value
            idea.hard_stop = chop_sl
            idea.version += 1
            session.commit()
            await self._handle_trade_closed(
                session, idea, attempt, open_pos, symbol,
                close_price, chop_sl, exit_reason="MT5_STOP",
                contract=pending_contract,
                hard_stop_at_close=chop_sl,
            )
            logger.info(
                f"[{symbol}] Idea {idea.id} filled @ {fill_price} and closed @ "
                f"{close_price} before worker tracked open position"
            )
            return True

        if outcome.status == "bad_fill":
            if outcome.position_ticket:
                pos = await self.bridge.get_position(outcome.position_ticket)
                pos_dir = position_direction(pos) if pos else None
                vol = getattr(pos, "volume", 0.0) if pos else 0.0
                if pos_dir and vol > 0:
                    close_res = await self.bridge.close_position(
                        ticket=outcome.position_ticket,
                        symbol=symbol,
                        direction=pos_dir.value,
                        volume=vol,
                    )
                    if close_res is None:
                        logger.error(
                            f"[{symbol}] Failed to close bad-fill position "
                            f"{outcome.position_ticket} for idea {idea.id} — will retry"
                        )
                        return True
            await self._requeue_pre_entry(
                session, idea, attempt, "BAD_FILL_AT_MARKET", "BAD_FILL_REQUEUE",
                {"fill_price": outcome.fill_price, "requested": attempt.entry_price},
            )
            return True

        if outcome.status == "cancelled":
            ticket = int(attempt.mt5_ticket or 0)
            if self._is_external_order_cancel(ticket, outcome):
                self._bot_cancelled_tickets.discard(ticket)
                await self._invalidate_external_order_cancel(
                    session, idea, attempt, outcome,
                )
                return True
            self._bot_cancelled_tickets.discard(ticket)
            return await self._requeue_pending_lost(
                session, idea, attempt, symbol, pending_contract,
                "MT5_ORDER_CANCELLED",
                extra={"ticket": attempt.mt5_ticket},
            )

        if outcome.status == "missing":
            return await self._requeue_pending_lost(
                session, idea, attempt, symbol, pending_contract,
                "PENDING_MISSING_REQUEUE",
                extra={"ticket": attempt.mt5_ticket},
            )

        return False

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
        pnl_dollars: float | None = None,
    ):
        """Record a closed attempt and transition idea state after exit."""
        if exit_reason == "MT5_STOP":
            hard_stop = hard_stop_at_close if hard_stop_at_close is not None else idea.hard_stop
            exit_reason = self._classify_stop_exit(
                idea.direction, close_price, chop_sl, hard_stop, contract
            )

        if pnl_dollars is None:
            pnl_dollars = contract.dollar_pnl(
                idea.direction, attempt.entry_price, close_price, attempt.volume
            )

        attempt.exit_price = close_price
        attempt.pnl = pnl_dollars
        attempt.closed_at = utcnow()
        attempt.exit_reason = exit_reason
        attempt.execution_state = ExecutionState.CLOSED.value
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
                await self._arm_reentry_pending(session, idea, symbol, contract)
        elif exit_reason == "TRAILING_STOP":
            idea.retries_used += 1
            self._log_event(session, idea.id, "EARLY_EXIT", {
                "pnl": pnl_dollars,
                "consumed_risk": idea.consumed_risk,
                "exit_reason": exit_reason,
                "chop_sl": chop_sl,
            })
            if idea.retries_used >= idea.max_retries:
                idea.state = TradeState.IDEA_INVALIDATED.value
                self._log_event(session, idea.id, "IDEA_INVALIDATED", {
                    "reason": "MAX_RETRIES", "retries_used": idea.retries_used,
                })
            else:
                idea.state = TradeState.WAITING_FOR_REENTRY.value
                idea.hard_stop = idea.original_hard_stop
                self._log_event(session, idea.id, "WAITING_FOR_REENTRY", {
                    "remaining_risk": idea.max_idea_risk - idea.consumed_risk
                })
                await asyncio.sleep(1.0)
                await self._arm_reentry_pending(session, idea, symbol, contract)
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
        TradeState.SUBMITTING_ORDER.value,
    })

    def _worker_sleep_sec(self, symbol: str, ideas: list) -> float:
        """Shorter loop when placing or monitoring pre-entry ideas (HFT-style wake)."""
        if symbol in self._ea_symbol_wake:
            self._ea_symbol_wake.discard(symbol)
            return 0.0
        fast = float(os.environ.get("WORKER_LOOP_FAST_SEC", "0.01"))
        idle = float(os.environ.get("WORKER_LOOP_IDLE_SEC", "0.1"))
        states = {getattr(i, "state", "") for i in ideas}
        if states & self._PRE_ENTRY_STATES:
            return fast
        if TradeState.TRADE_OPEN.value in states:
            return fast
        return idle

    async def _on_ea_trade_event(self, msg: dict) -> None:
        """Wake symbol workers when the EA pushes fill/close events."""
        symbol = str(msg.get("symbol", "") or "")
        event = str(msg.get("event", "") or "")
        if not symbol:
            return
        self._ea_symbol_wake.add(symbol)
        if event in ("POSITION_OPENED", "POSITION_CLOSED", "DEAL_ADD", "ORDER_ADD"):
            self._ea_force_resolve_symbols.add(symbol)
            logger.debug("EA trade event %s on %s — fast resolve wake", event, symbol)

    async def start(self):
        self.running = True
        logger.info("Starting Modular Trade Manager...")
        
        if isinstance(self.bridge, EABridge):
            self.bridge.set_trade_event_handler(self._on_ea_trade_event)
        
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

    def _match_orphan_order_to_attempt(self, session, order) -> tuple[TradeIdea, TradeAttempt] | None:
        """Link an untracked MT5 pending order to a DB attempt without a ticket."""
        symbol = getattr(order, "symbol", "")
        if not symbol:
            return None

        ideas = session.query(TradeIdea).filter(
            TradeIdea.symbol == symbol,
            TradeIdea.state.in_([
                TradeState.SUBMITTING_ORDER.value,
                TradeState.PENDING_ORDER_PLACED.value,
                TradeState.WAITING_FOR_SETUP.value,
                TradeState.WAITING_FOR_REENTRY.value,
            ]),
        ).all()

        for idea in ideas:
            attempt = session.query(TradeAttempt).filter(
                TradeAttempt.trade_idea_id == idea.id,
                TradeAttempt.execution_state.in_([
                    ExecutionState.PENDING.value,
                    ExecutionState.SUBMITTED.value,
                ]),
                or_(TradeAttempt.mt5_ticket.is_(None), TradeAttempt.mt5_ticket == 0),
            ).order_by(TradeAttempt.id.desc()).first()
            if attempt is None:
                continue

            spec = SymbolContract.for_symbol(symbol)
            if order_matches_attempt(
                order,
                symbol=symbol,
                direction=idea.direction,
                entry_price=attempt.entry_price,
                volume=attempt.volume,
                tick_size=spec.tick_size,
            ):
                return idea, attempt
        return None

    async def _reconcile_orphan_orders_on_startup(self, session) -> None:
        """Attach live MT5 pending orders that have no ticket in the DB."""
        mt5_orders = await self.bridge.get_orders()
        if not mt5_orders:
            return

        known_tickets = {
            int(att.mt5_ticket)
            for att in session.query(TradeAttempt).filter(
                TradeAttempt.mt5_ticket.isnot(None),
                TradeAttempt.mt5_ticket > 0,
            ).all()
        }

        for order in mt5_orders:
            ticket = int(getattr(order, "ticket", 0) or 0)
            if not ticket or ticket in known_tickets:
                continue

            match = self._match_orphan_order_to_attempt(session, order)
            if match is None:
                logger.warning(
                    f"Untracked MT5 pending order {ticket} on "
                    f"{getattr(order, 'symbol', '?')} — no DB match; cancelling"
                )
                await self.bridge.cancel_pending_order(ticket)
                continue

            idea, attempt = match
            attempt.mt5_ticket = ticket
            attempt.execution_state = ExecutionState.SUBMITTED.value
            idea.state = TradeState.PENDING_ORDER_PLACED.value
            self._log_event(session, idea.id, "ORPHAN_ORDER_RECOVERED", {
                "ticket": ticket,
                "symbol": idea.symbol,
                "entry": attempt.entry_price,
                "volume": attempt.volume,
            })
            idea.version += 1
            known_tickets.add(ticket)
            logger.info(
                f"Recovered orphan MT5 order {ticket} → idea {idea.id} "
                f"attempt #{attempt.attempt_number}"
            )
        session.flush()

    async def _reconcile_positions_on_startup(self):
        """Reconcile DB open positions with MT5 on startup."""
        if self.bridge.connection_state != ConnectionState.ACTIVE:
            return

        with self.SessionLocal() as session:
            await self._reconcile_orphan_orders_on_startup(session)

            mt5_positions = await self.bridge.get_positions()
            mt5_tickets = {p.ticket: p for p in mt5_positions} if mt5_positions else {}

            db_positions = session.query(OpenPosition).all()
            for db_pos in db_positions:
                if db_pos.mt5_ticket not in mt5_tickets:
                    logger.warning(
                        f"Position {db_pos.mt5_ticket} missing in MT5. "
                        f"Resolving close from deal history."
                    )
                    idea = session.get(TradeIdea, db_pos.trade_idea_id)
                    attempt = session.query(TradeAttempt).filter_by(
                        trade_idea_id=idea.id,
                        execution_state=ExecutionState.FILLED.value,
                    ).order_by(TradeAttempt.id.desc()).first()

                    if idea and attempt:
                        contract = SymbolContract.for_symbol(db_pos.symbol)
                        chop_sl = self.config.chop_exit_price(
                            db_pos.symbol, attempt.entry_price, idea.direction
                        )
                        close_details = await self.bridge.get_position_close_details(
                            db_pos.mt5_ticket
                        )
                        if close_details is not None:
                            close_price = close_details.close_price
                            broker_pnl = close_details.net_profit
                        else:
                            close_price = await self.bridge.get_position_close_price(
                                db_pos.mt5_ticket
                            )
                            broker_pnl = None
                            if close_price is None:
                                close_price = attempt.entry_price
                                logger.warning(
                                    f"No deal history for position {db_pos.mt5_ticket}; "
                                    f"using entry price as close"
                                )

                        await self._handle_trade_closed(
                            session, idea, attempt, db_pos, db_pos.symbol,
                            close_price, chop_sl, exit_reason="MT5_STOP",
                            contract=contract,
                            hard_stop_at_close=db_pos.current_stop or idea.hard_stop,
                            pnl_dollars=broker_pnl,
                        )
                        self._log_event(session, idea.id, "RECONCILIATION_CLOSED", {
                            "ticket": db_pos.mt5_ticket,
                            "close_price": close_price,
                            "pnl": broker_pnl,
                        })
                    else:
                        session.delete(db_pos)

            # Recover ideas stuck mid-submit from a crash
            stuck_ideas = session.query(TradeIdea).filter_by(
                state=TradeState.SUBMITTING_ORDER.value,
            ).all()
            for idea in stuck_ideas:
                logger.warning(
                    f"Idea {idea.id} stuck in SUBMITTING_ORDER — cancelling inflight attempts"
                )
                submitted = session.query(TradeAttempt).filter_by(
                    trade_idea_id=idea.id,
                    execution_state=ExecutionState.SUBMITTED.value,
                ).all()
                for att in submitted:
                    if att.mt5_ticket:
                        cancel_res = await self._cancel_pending_order_as_bot(att.mt5_ticket)
                        if cancel_res is None:
                            logger.warning(
                                f"Crash recovery: could not cancel MT5 order "
                                f"{att.mt5_ticket} for idea {idea.id} — leaving SUBMITTED"
                            )
                            continue
                    att.execution_state = ExecutionState.CANCELLED.value
                    att.exit_reason = "CRASH_RECOVERY"
                pending = session.query(TradeAttempt).filter_by(
                    trade_idea_id=idea.id,
                    execution_state=ExecutionState.PENDING.value,
                ).all()
                still_submitted = session.query(TradeAttempt).filter_by(
                    trade_idea_id=idea.id,
                    execution_state=ExecutionState.SUBMITTED.value,
                ).count()
                if still_submitted > 0:
                    idea.state = TradeState.PENDING_ORDER_PLACED.value
                else:
                    for att in pending:
                        session.delete(att)
                    idea.state = self._pre_entry_waiting_state(idea)
                self._log_event(session, idea.id, "CRASH_RECOVERED")
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
                            TradeState.SUBMITTING_ORDER.value,
                            TradeState.PENDING_ORDER_PLACED.value,
                            TradeState.TRADE_OPEN.value,
                            TradeState.WAITING_FOR_REENTRY.value,
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
            req: ExecutionRequest | None = None
            try:
                req = await self.execution_queue.get()

                if self.liquidity_filter.is_rollover_period():
                    logger.warning(f"OrderExecutor: Holding execution for {req.symbol} due to Rollover trap.")
                    await asyncio.sleep(1.0)
                    await self.execution_queue.put(req)
                    continue

                logger.info(f"Executing pending order for {req.symbol}")
                live_tick = await self.bridge.get_tick(req.symbol)
                if live_tick is None:
                    logger.error(f"No tick for {req.symbol}; re-queuing order")
                    await asyncio.sleep(0.5)
                    await self.execution_queue.put(req)
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

                order_ticket = 0
                if result and result.retcode == 10009:
                    order_ticket = int(getattr(result, "order", 0) or 0)

                with self.SessionLocal() as session:
                    idea = session.get(TradeIdea, req.idea_id)
                    if idea is None:
                        logger.error(f"[{req.symbol}] Idea {req.idea_id} not found for execution")
                        if order_ticket:
                            await self._cancel_pending_order_as_bot(order_ticket)
                        continue
                    attempt = session.query(TradeAttempt).filter_by(
                        trade_idea_id=idea.id,
                        execution_state=ExecutionState.PENDING.value,
                    ).order_by(TradeAttempt.id.desc()).first()
                    if attempt is None:
                        attempt = session.query(TradeAttempt).filter_by(
                            trade_idea_id=idea.id, attempt_number=req.attempt_number
                        ).first()
                    if attempt is None:
                        logger.error(
                            f"[{req.symbol}] No PENDING attempt for idea {idea.id} "
                            f"(attempt_number={req.attempt_number})"
                        )
                        if order_ticket:
                            logger.warning(
                                f"[{req.symbol}] Cancelling orphan MT5 order {order_ticket} "
                                f"(no DB attempt for idea {idea.id})"
                            )
                            await self._cancel_pending_order_as_bot(order_ticket)
                        idea.state = self._pre_entry_waiting_state(idea)
                        idea.version += 1
                        session.commit()
                        continue

                    if order_ticket:
                        attempt.execution_state = ExecutionState.SUBMITTED.value
                        attempt.mt5_ticket = order_ticket
                        idea.state = TradeState.PENDING_ORDER_PLACED.value
                        idea.version += 1
                        session.commit()

                        entry_plan = self.bridge.plan_pending_entry(
                            req.direction, req.price, live_bid, live_ask,
                            tick_size=tick_size,
                        )
                        self._log_event(session, idea.id, "PENDING_ORDER_PLACED", {
                            "ticket": attempt.mt5_ticket,
                            "entry_price": req.price,
                            "chop_sl": req.sl,
                            "order_kind": entry_plan.kind.value,
                        })
                        idea.version += 1
                        session.commit()
                    else:
                        retcode = getattr(result, "retcode", None) if result else None
                        comment = getattr(result, "comment", "") if result else ""
                        session.delete(attempt)
                        if result is None:
                            idea.state = self._pre_entry_waiting_state(idea)
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
                        elif retcode == 10027:
                            idea.state = self._pre_entry_waiting_state(idea)
                            self._log_event(session, idea.id, "PENDING_DEFERRED", {
                                "entry": req.price,
                                "reason": "AUTOTRADING_DISABLED",
                                "retcode": retcode,
                            })
                            logger.error(
                                f"[{req.symbol}] Idea {idea.id} blocked: enable Algo Trading "
                                f"in MT5 (retcode=10027)"
                            )
                        else:
                            idea.state = TradeState.IDEA_INVALIDATED.value
                            self._log_event(session, idea.id, "ENTRY_REJECTED", {
                                "reason": f"REJECTED: {retcode}",
                                "comment": comment,
                            })
                            logger.error(
                                f"[{req.symbol}] Idea {idea.id} entry rejected: "
                                f"retcode={retcode} {comment}"
                            )

                    idea.version += 1
                    session.commit()

                # If the pending order fills immediately, ensure chop SL is on the position
                if order_ticket:
                    chop_sl = req.sl
                    immediate_pos_ticket = None
                    for _ in range(20):
                        positions = await self.bridge.get_positions(req.symbol)
                        pos_ticket = self._resolve_position_ticket(
                            positions or [], order_ticket,
                            direction=req.direction, volume=req.volume,
                            entry_price=req.price,
                        )
                        if pos_ticket is not None:
                            immediate_pos_ticket = pos_ticket
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

                    if immediate_pos_ticket is not None:
                        spec = await self.bridge.get_symbol_spec(req.symbol)
                        fill_contract = (
                            SymbolContract.from_mt5_spec(req.symbol, spec)
                            if spec else SymbolContract.for_symbol(req.symbol)
                        )
                        with self.SessionLocal() as session:
                            idea = session.get(TradeIdea, req.idea_id)
                            attempt = session.query(TradeAttempt).filter_by(
                                trade_idea_id=req.idea_id,
                                execution_state=ExecutionState.SUBMITTED.value,
                            ).order_by(TradeAttempt.id.desc()).first()
                            if idea and attempt:
                                await self._register_trade_open(
                                    session, idea, attempt, req.symbol,
                                    immediate_pos_ticket, fill_contract,
                                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Execution loop error: {e}", exc_info=True)
            finally:
                if req is not None:
                    self.execution_queue.task_done()

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
                            TradeState.SUBMITTING_ORDER.value,
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

                            spec = await self.bridge.get_symbol_spec(symbol)
                            if spec is None:
                                logger.warning(f"Could not get symbol spec for {symbol}. MT5 disconnected? Skipping tick.")
                                continue
                            contract = SymbolContract.from_mt5_spec(symbol, spec)

                            latest_attempt = session.query(TradeAttempt).filter_by(
                                trade_idea_id=idea.id,
                            ).order_by(TradeAttempt.id.desc()).first()
                            if await self._try_recover_open_position(
                                session, idea, symbol, latest_attempt, contract
                            ):
                                continue

                            inflight = session.query(TradeAttempt).filter(
                                TradeAttempt.trade_idea_id == idea.id,
                                TradeAttempt.execution_state.in_([
                                    ExecutionState.PENDING.value,
                                    ExecutionState.SUBMITTED.value,
                                ]),
                            ).first()
                            if inflight:
                                continue

                            open_positions = await self.bridge.get_positions(symbol)
                            if open_positions is None:
                                continue
                            if await self._recover_or_block_reentry(
                                session, idea, symbol, open_positions
                            ):
                                continue
                            
                            if not self.session_manager.is_in_prime_session(symbol):
                                continue # Wait for prime session to place the order

                            tick = await self.bridge.get_tick(symbol)
                            if tick is None:
                                continue
                            market = MarketTick.from_mt5(tick)
                            if not market.is_valid():
                                continue

                            if idea.state == TradeState.WAITING_FOR_SETUP.value:
                                if not ready_for_initial_entry_placement(
                                    idea.direction,
                                    idea.original_entry,
                                    market.bid,
                                    market.ask,
                                    idea.entry_zone_low,
                                    idea.entry_zone_high,
                                    contract.tick_size,
                                ):
                                    if idea.id not in self._entry_zone_defer_logged:
                                        self._entry_zone_defer_logged.add(idea.id)
                                        self._log_event(session, idea.id, "PENDING_DEFERRED", {
                                            "reason": "entry_zone_or_immediate_fill",
                                            "entry": idea.original_entry,
                                            "bid": market.bid,
                                            "ask": market.ask,
                                            "zone_low": idea.entry_zone_low,
                                            "zone_high": idea.entry_zone_high,
                                        })
                                        idea.version += 1
                                        session.commit()
                                        logger.info(
                                            f"[{symbol}] Idea {idea.id} waiting for entry zone "
                                            f"or restable price @ {idea.original_entry} "
                                            f"(bid={market.bid}, ask={market.ask})"
                                        )
                                    continue

                            remaining_risk = idea.max_idea_risk - idea.consumed_risk
                            if remaining_risk <= 0:
                                logger.warning(
                                    f"[{symbol}] Idea {idea.id} has no remaining risk budget "
                                    f"(consumed={idea.consumed_risk:.2f}, max={idea.max_idea_risk:.2f})"
                                )
                                continue

                            account_info = await self.bridge.get_account_info()

                            account_equity = getattr(account_info, 'equity', None)

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

                            volume, volume_source = PositionSizingEngine.resolve_volume(
                                contract,
                                remaining_risk,
                                idea.original_entry,
                                idea.original_hard_stop,
                                predefined_lot=idea.lot_size,
                                max_lot_size=PositionSizingEngine.max_lot_size_from_env(),
                            )
                            logger.info(
                                f"[{symbol}] Idea {idea.id} volume {volume} "
                                f"({volume_source}, remaining_risk={remaining_risk:.2f})"
                            )
                            
                            chop_sl = self.config.chop_exit_price(
                                symbol, idea.original_entry, idea.direction
                            )

                            entry_plan = self.bridge.plan_pending_entry(
                                idea.direction, idea.original_entry,
                                market.bid, market.ask,
                                tick_size=contract.tick_size,
                            )
                            if idea.state == TradeState.WAITING_FOR_REENTRY.value:
                                await self._cancel_mismatched_reentry_pending(
                                    session, idea, symbol, entry_plan, contract.tick_size
                                )
                                price_side = entry_market_side(
                                    idea.direction, idea.original_entry,
                                    market.bid, market.ask, contract.tick_size,
                                )
                                logger.info(
                                    f"[{symbol}] Idea {idea.id} re-entry pending: "
                                    f"{entry_plan.kind.value} @ {idea.original_entry} "
                                    f"({price_side}, bid={market.bid}, ask={market.ask})"
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
                            
                            attempt_num = self._next_attempt_number(session, idea.id)
                            attempt = TradeAttempt(
                                trade_idea_id=idea.id,
                                attempt_number=attempt_num,
                                execution_state=ExecutionState.PENDING.value,
                                entry_price=idea.original_entry,
                                volume=volume
                            )
                            session.add(attempt)

                            self._entry_zone_defer_logged.discard(idea.id)
                            idea.state = TradeState.SUBMITTING_ORDER.value
                            idea.version += 1
                            session.commit()

                            req = ExecutionRequest(
                                idea_id=idea.id, symbol=idea.symbol, direction=idea.direction,
                                volume=volume, price=idea.original_entry,
                                sl=chop_sl, tp=idea.take_profit,
                                attempt_number=attempt_num,
                                current_price=market.bid,
                                bid=market.bid, ask=market.ask,
                            )
                            await self.execution_queue.put(req)
                            continue

                        elif idea.state == TradeState.SUBMITTING_ORDER.value:
                            inflight = session.query(TradeAttempt).filter(
                                TradeAttempt.trade_idea_id == idea.id,
                                TradeAttempt.execution_state.in_([
                                    ExecutionState.PENDING.value,
                                    ExecutionState.SUBMITTED.value,
                                ]),
                            ).first()
                            if not inflight:
                                idea.state = self._pre_entry_waiting_state(idea)
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

                            # Resolve fill / close / cancel before limit↔stop maintenance
                            outcome = None
                            force_resolve = symbol in self._ea_force_resolve_symbols
                            if force_resolve:
                                self._ea_force_resolve_symbols.discard(symbol)
                            max_iters = 5 if force_resolve else 60
                            poll_sleep = 0.02 if force_resolve else 0.1
                            for _ in range(max_iters):
                                outcome = await self.bridge.resolve_pending_order_outcome(
                                    symbol=symbol,
                                    order_ticket=attempt.mt5_ticket,
                                    direction=idea.direction,
                                    volume=attempt.volume,
                                    entry_price=attempt.entry_price,
                                    tick_size=pending_contract.tick_size,
                                )
                                if outcome.status != "missing":
                                    break
                                await asyncio.sleep(poll_sleep)

                            if outcome and outcome.status != "pending":
                                if await self._handle_pending_order_outcome(
                                    session, idea, attempt, symbol, outcome, pending_contract
                                ):
                                    continue

                            # Refresh book after outcome poll
                            orders = await self.bridge.get_orders(symbol)
                            if orders is None:
                                continue

                            expected_type = self.bridge.expected_pending_order_type(
                                idea.direction, idea.original_entry, live_bid, live_ask,
                                tick_size=pending_contract.tick_size,
                            )
                            expected_plan = self.bridge.plan_pending_entry(
                                idea.direction, idea.original_entry, live_bid, live_ask,
                                tick_size=pending_contract.tick_size,
                            )
                            stale_type = False
                            current_order = None
                            for o in orders:
                                if getattr(o, 'ticket', 0) == attempt.mt5_ticket:
                                    current_order = o
                                    if expected_type is not None and getattr(o, 'type', None) != expected_type:
                                        stale_type = True
                                    break

                            if stale_type:
                                cancel_res = await self._cancel_pending_order_as_bot(
                                    attempt.mt5_ticket
                                )
                                if cancel_res is None:
                                    logger.warning(
                                        f"[{symbol}] Could not cancel pending {attempt.mt5_ticket} "
                                        f"for type replacement — checking fill history"
                                    )
                                    if await self._try_recover_open_position(
                                        session, idea, symbol, attempt, pending_contract
                                    ):
                                        continue
                                    retry_outcome = await self.bridge.resolve_pending_order_outcome(
                                        symbol=symbol,
                                        order_ticket=attempt.mt5_ticket,
                                        direction=idea.direction,
                                        volume=attempt.volume,
                                        entry_price=attempt.entry_price,
                                        tick_size=pending_contract.tick_size,
                                    )
                                    if await self._handle_pending_order_outcome(
                                        session, idea, attempt, symbol,
                                        retry_outcome, pending_contract,
                                    ):
                                        continue
                                    logger.warning(
                                        f"[{symbol}] Cancel failed for pending "
                                        f"{attempt.mt5_ticket} — keeping SUBMITTED, will retry"
                                    )
                                    continue
                                await self._requeue_pre_entry(
                                    session, idea, attempt, "ORDER_TYPE_STALE",
                                    "ORDER_TYPE_STALE",
                                    {
                                        "ticket": attempt.mt5_ticket,
                                        "expected_type": expected_type,
                                        "order_kind": expected_plan.kind.value,
                                        "price_side": entry_market_side(
                                            idea.direction, idea.original_entry,
                                            live_bid, live_ask, pending_contract.tick_size,
                                        ),
                                        "had_type": getattr(current_order, 'type', None),
                                        "bid": live_bid,
                                        "ask": live_ask,
                                    },
                                )
                                continue

                            if outcome and outcome.status == "pending":
                                continue

                            if outcome and await self._handle_pending_order_outcome(
                                session, idea, attempt, symbol, outcome, pending_contract
                            ):
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
                                if close_res is None:
                                    logger.error(
                                        f"[{symbol}] Failed to close position "
                                        f"{open_pos.mt5_ticket} for idea {idea.id} — will retry"
                                    )
                                    continue

                                close_price = getattr(close_res, 'price', mark_price)

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

            with self.SessionLocal() as _sleep_session:
                sleep_ideas = _sleep_session.query(TradeIdea).filter(
                    TradeIdea.symbol == symbol,
                    TradeIdea.state.in_([
                        TradeState.WAITING_FOR_SETUP.value,
                        TradeState.PENDING_ORDER_PLACED.value,
                        TradeState.WAITING_FOR_REENTRY.value,
                        TradeState.TRADE_OPEN.value,
                        TradeState.SUBMITTING_ORDER.value,
                    ]),
                ).all()
            await asyncio.sleep(self._worker_sleep_sec(symbol, sleep_ideas))

if __name__ == "__main__":
    async def _run():
        manager = TradeManager()
        try:
            await manager.start()
        finally:
            await manager.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
