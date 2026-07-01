import asyncio
import logging
import os
import time
from enum import Enum
from typing import Optional, Any

from src.market import MarketTick, SymbolContract, position_stop_matches
from src.market.order_outcome import PendingOrderOutcome, resolve_pending_order_outcome
from src.market.deal_history import PositionCloseDetails, close_details_from_deals
from src.market.pending_entry import (
    PendingOrderKind,
    fill_price_violates_entry,
    plan_pending_entry,
)

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"


class MT5Bridge:
    """Async bridge to MT5 via mt5linux (RPyC over Wine).

    All public methods are async and run blocking MT5 calls in a thread executor
    to avoid freezing the asyncio event loop.
    """

    def __init__(self, host: str | None = None, port: int | None = None,
                 magic=234000, deviation=20, max_retries=3):
        self.host = host or os.environ.get("MT5_HOST", "localhost")
        self.port = port if port is not None else int(os.environ.get("MT5_PORT", "18812"))
        self.magic = magic
        self.deviation = deviation
        self.max_order_retries = max_retries
        self.connection_state = ConnectionState.DISCONNECTED
        self._mt5 = None
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._last_successful_call = 0.0
        self._degraded_timeout = 30.0  # seconds without successful call → DEGRADED

    # ── Import mt5linux lazily to allow tests without the library ──

    def _get_mt5(self):
        if self._mt5 is None:
            try:
                from mt5linux import MetaTrader5
                self._mt5 = MetaTrader5(host=self.host, port=self.port)
            except ImportError:
                logger.error("mt5linux not installed. MT5 bridge unavailable.")
                return None
            except Exception as e:
                logger.error(f"MT5 RPyC connection failed: {e}")
                return None
        return self._mt5

    # ── Connection management ─────────────────────────────────────

    def _sync_login_if_configured(self, mt5) -> bool:
        """Optional login via MT5_LOGIN, MT5_PASSWORD, MT5_SERVER env vars."""
        login = os.environ.get("MT5_LOGIN", "").strip()
        password = os.environ.get("MT5_PASSWORD", "")
        server = os.environ.get("MT5_SERVER", "").strip()
        if not login or not password or not server:
            return True
        try:
            ok = mt5.login(int(login), password=password, server=server)
        except (TypeError, ValueError) as exc:
            logger.error(f"MT5_LOGIN must be numeric: {exc}")
            return False
        if not ok:
            logger.error(f"MT5 login failed for {login}@{server}: {mt5.last_error()}")
            return False
        logger.info(f"Logged into MT5 account {login} @ {server}")
        return True

    def _sync_connect(self) -> bool:
        mt5 = self._get_mt5()
        if mt5 is None:
            return False
        try:
            if mt5.initialize():
                if not self._sync_login_if_configured(mt5):
                    self.connection_state = ConnectionState.DISCONNECTED
                    return False
                info = mt5.account_info()
                if info:
                    logger.info(
                        f"MT5 account {getattr(info, 'login', '?')} "
                        f"balance={getattr(info, 'balance', '?')} "
                        f"leverage=1:{getattr(info, 'leverage', '?')}"
                    )
                self.connection_state = ConnectionState.ACTIVE
                self._last_successful_call = time.monotonic()
                self._reconnect_attempts = 0
                logger.info(f"Connected to MT5 via mt5linux on {self.host}:{self.port}")
                return True
            else:
                logger.error(f"MT5 init failed: {mt5.last_error()}")
                self.connection_state = ConnectionState.DISCONNECTED
                return False
        except Exception as e:
            logger.error(f"MT5 connection exception: {e}")
            self.connection_state = ConnectionState.DISCONNECTED
            return False

    async def connect(self) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_connect)

    async def reconnect(self) -> bool:
        """Attempt reconnection with exponential backoff."""
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.error("Max reconnection attempts reached.")
            return False
        delay = min(2 ** self._reconnect_attempts, 60)
        self._reconnect_attempts += 1
        logger.info(f"Reconnecting in {delay}s (attempt {self._reconnect_attempts})...")
        await asyncio.sleep(delay)
        return await self.connect()

    def _sync_shutdown(self):
        mt5 = self._get_mt5()
        if mt5 and self.connection_state != ConnectionState.DISCONNECTED:
            try:
                mt5.shutdown()
            except Exception:
                pass
        self.connection_state = ConnectionState.DISCONNECTED
        logger.info("MT5 connection closed.")

    async def shutdown(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._sync_shutdown)

    def _check_connection(self) -> bool:
        """Update connection state based on time since last successful call."""
        if self.connection_state == ConnectionState.DISCONNECTED:
            return False
        elapsed = time.monotonic() - self._last_successful_call
        if elapsed > self._degraded_timeout:
            if self.connection_state != ConnectionState.DEGRADED:
                logger.warning("MT5 connection DEGRADED — no successful call recently.")
                self.connection_state = ConnectionState.DEGRADED
        return True

    def _mark_success(self):
        self._last_successful_call = time.monotonic()
        if self.connection_state == ConnectionState.DEGRADED:
            self.connection_state = ConnectionState.ACTIVE

    # ── Price data ────────────────────────────────────────────────

    def _sync_get_tick(self, symbol: str) -> Optional[Any]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None:
                self._mark_success()
            return tick
        except Exception as e:
            logger.error(f"get_tick({symbol}) error: {e}")
            self.connection_state = ConnectionState.DISCONNECTED
            return None

    async def get_tick(self, symbol: str) -> Optional[Any]:
        """Get current tick (bid/ask) for a symbol."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_tick, symbol)

    def _sync_get_symbol_spec(self, symbol: str) -> Optional[Any]:
        """Get contract specification (tick_value, lot_min, lot_max, lot_step, digits)."""
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None
        try:
            info = mt5.symbol_info(symbol)
            if info is not None:
                self._mark_success()
            return info
        except Exception as e:
            logger.error(f"get_symbol_spec({symbol}) error: {e}")
            return None

    async def get_symbol_spec(self, symbol: str) -> Optional[Any]:
        """Get contract specification for a symbol."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_symbol_spec, symbol)

    # ── Account info ──────────────────────────────────────────────

    def _sync_get_account_info(self) -> Optional[Any]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None
        try:
            info = mt5.account_info()
            if info is not None:
                self._mark_success()
            return info
        except Exception as e:
            logger.error(f"get_account_info error: {e}")
            return None

    async def get_account_info(self) -> Optional[Any]:
        """Get account balance, equity, margin info."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_account_info)

    # ── Order placement ───────────────────────────────────────────

    def _sync_place_order(self, symbol: str, direction: str, volume: float,
                          price: float, sl: float, tp: float) -> Optional[Any]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None

        type_dict = {"BUY": mt5.ORDER_TYPE_BUY, "SELL": mt5.ORDER_TYPE_SELL}
        order_type = type_dict.get(direction.upper())
        if order_type is None:
            logger.error(f"Invalid direction: {direction}")
            return None

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "TradeIdeaBot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        for attempt in range(1, self.max_order_retries + 1):
            try:
                result = mt5.order_send(request)
                if result is None:
                    logger.error(f"order_send returned None (attempt {attempt})")
                    continue
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    self._mark_success()
                    return result
                logger.warning(
                    f"Order failed attempt {attempt}/{self.max_order_retries}: "
                    f"retcode={result.retcode}, comment={getattr(result, 'comment', '')}"
                )
                if attempt < self.max_order_retries:
                    time.sleep(0.5 * attempt)
            except Exception as e:
                logger.error(f"order_send exception (attempt {attempt}): {e}")
                if attempt < self.max_order_retries:
                    time.sleep(0.5 * attempt)
        return None

    async def place_order(self, symbol: str, direction: str, volume: float,
                          price: float, sl: float, tp: float) -> Optional[Any]:
        """Place a market order with retry logic. Returns MT5 result or None."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._sync_place_order, symbol, direction, volume, price, sl, tp
        )

    # ── Position closing ──────────────────────────────────────────

    def _sync_close_position(self, ticket: int, symbol: str, direction: str,
                             volume: float) -> Optional[Any]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None

        # Close = opposite direction deal
        close_type = mt5.ORDER_TYPE_SELL if direction.upper() == "BUY" else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"Cannot get tick for close: {symbol}")
            return None

        price = MarketTick.from_mt5(tick).close_price(direction)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "TradeIdeaBot_Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        for attempt in range(1, self.max_order_retries + 1):
            try:
                result = mt5.order_send(request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self._mark_success()
                    return result
                logger.warning(f"Close failed attempt {attempt}: {getattr(result, 'retcode', 'None')}")
                if attempt < self.max_order_retries:
                    time.sleep(0.5 * attempt)
            except Exception as e:
                logger.error(f"close_position exception (attempt {attempt}): {e}")
                if attempt < self.max_order_retries:
                    time.sleep(0.5 * attempt)
        return None

    async def close_position(self, ticket: int, symbol: str, direction: str,
                             volume: float) -> Optional[Any]:
        """Close an open position by ticket."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._sync_close_position, ticket, symbol, direction, volume
        )

    # ── Position modification (trailing stop) ─────────────────────

    def _sync_modify_position(self, ticket: int, symbol: str,
                              sl: float, tp: float) -> Optional[Any]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": ticket,
            "sl": sl,
            "tp": tp,
            "magic": self.magic,
        }

        try:
            result = mt5.order_send(request)
            if result is None:
                return None
            retcode = result.retcode
            # 10025 = NO_CHANGES — SL/TP already at requested levels (e.g. from pending order)
            if retcode in (mt5.TRADE_RETCODE_DONE, 10025):
                self._mark_success()
                return result
            logger.warning(f"Modify position {ticket} failed: {retcode}")
        except Exception as e:
            logger.error(f"modify_position exception: {e}")
        return None

    async def modify_position(self, ticket: int, symbol: str,
                              sl: float, tp: float) -> Optional[Any]:
        """Modify SL/TP on an open position (for trailing stop updates)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._sync_modify_position, ticket, symbol, sl, tp
        )

    # ── Position queries ──────────────────────────────────────────

    def _sync_get_positions(self, symbol: str = None) -> Optional[list]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None
        try:
            if symbol:
                positions = mt5.positions_get(symbol=symbol)
            else:
                positions = mt5.positions_get()
            if positions is not None:
                self._mark_success()
                # Filter to our magic number
                return [p for p in positions if p.magic == self.magic]
            return None
        except Exception as e:
            logger.error(f"get_positions error: {e}")
            return None

    async def get_positions(self, symbol: str = None) -> Optional[list]:
        """Get open positions, optionally filtered by symbol. Only returns our magic number. Returns None on error."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_positions, symbol)

    def _sync_get_position(self, ticket: int) -> Optional[Any]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None
        try:
            positions = mt5.positions_get(ticket=ticket)
            if positions:
                self._mark_success()
                return positions[0]
        except Exception as e:
            logger.error(f"get_position error: {e}")
        return None

    async def get_position(self, ticket: int) -> Optional[Any]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_position, ticket)

    @staticmethod
    def position_stop_matches(
        position,
        sl: float,
        tick_size: float = 0.01,
        tick_value: float = 1.0,
        symbol: str = "",
    ) -> bool:
        contract = SymbolContract(symbol=symbol, tick_size=tick_size, tick_value=tick_value)
        return position_stop_matches(position, sl, contract)

    def _sync_get_position_close_details(
        self, position_ticket: int
    ) -> Optional[PositionCloseDetails]:
        """Return broker close price and PnL from deal history."""
        mt5 = self._get_mt5()
        if mt5 is None:
            return None
        try:
            deals = mt5.history_deals_get(position=position_ticket)
            if not deals:
                return None
            details = close_details_from_deals(list(deals))
            if details is not None:
                self._mark_success()
            return details
        except Exception as e:
            logger.error(f"get_position_close_details error: {e}")
        return None

    async def get_position_close_details(
        self, position_ticket: int
    ) -> Optional[PositionCloseDetails]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._sync_get_position_close_details, position_ticket
        )

    def _sync_get_position_close_price(self, position_ticket: int) -> Optional[float]:
        """Return the broker fill price for the most recent close deal on a position."""
        details = self._sync_get_position_close_details(position_ticket)
        if details is not None:
            return details.close_price
        return None

    async def get_position_close_price(self, position_ticket: int) -> Optional[float]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._sync_get_position_close_price, position_ticket
        )

    # ── Pending orders ────────────────────────────────────────────

    def _mt5_pending_type_map(self, mt5) -> dict[PendingOrderKind, int]:
        return {
            PendingOrderKind.BUY_LIMIT: mt5.ORDER_TYPE_BUY_LIMIT,
            PendingOrderKind.BUY_STOP: mt5.ORDER_TYPE_BUY_STOP,
            PendingOrderKind.SELL_LIMIT: mt5.ORDER_TYPE_SELL_LIMIT,
            PendingOrderKind.SELL_STOP: mt5.ORDER_TYPE_SELL_STOP,
        }

    def plan_pending_entry(
        self, direction: str, entry_price: float, bid: float, ask: float,
        tick_size: float = 0.01,
    ):
        mt5 = self._get_mt5()
        type_map = self._mt5_pending_type_map(mt5) if mt5 else None
        return plan_pending_entry(
            direction, entry_price, bid, ask, tick_size=tick_size,
            mt5_type_map=type_map,
        )

    def expected_pending_order_type(
        self, direction: str, entry_price: float, bid: float, ask: float,
        tick_size: float = 0.01,
    ) -> int | None:
        """Return the MT5 pending order type for entry given live bid/ask."""
        plan = self.plan_pending_entry(
            direction, entry_price, bid, ask, tick_size=tick_size
        )
        return plan.mt5_order_type

    def _pending_would_fill_immediately(
        self, mt5, direction: str, entry_price: float,
        bid: float, ask: float, order_type: int,
        tick_size: float = 0.01,
    ) -> bool:
        plan = self.plan_pending_entry(
            direction, entry_price, bid, ask, tick_size=tick_size
        )
        if plan.mt5_order_type != order_type:
            return True
        return plan.would_fill_immediately

    def _sync_verify_pending_resting(
        self, symbol: str, order_ticket: int, direction: str,
        entry_price: float, volume: float, tick_size: float,
    ) -> tuple[bool, str]:
        """After placement, confirm order is pending (not an immediate off-entry fill)."""
        mt5 = self._get_mt5()
        if mt5 is None:
            return False, "mt5_unavailable"

        time.sleep(0.2)
        orders = mt5.orders_get(symbol=symbol)
        if orders and any(getattr(o, "ticket", 0) == order_ticket for o in orders):
            return True, "pending"

        positions = mt5.positions_get(symbol=symbol) or []
        ours = [p for p in positions if getattr(p, "magic", 0) == self.magic]
        for p in ours:
            if abs(getattr(p, "volume", 0.0) - volume) > 0.0001:
                continue
            fill = float(getattr(p, "price_open", 0.0))
            if fill_price_violates_entry(direction, entry_price, fill, tick_size):
                ticket = int(getattr(p, "ticket", 0))
                close_type = mt5.ORDER_TYPE_SELL if direction.upper() == "BUY" else mt5.ORDER_TYPE_BUY
                tick = mt5.symbol_info_tick(symbol)
                price = tick.bid if direction.upper() == "BUY" else tick.ask
                mt5.order_send({
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": volume,
                    "type": close_type,
                    "position": ticket,
                    "price": price,
                    "deviation": self.deviation,
                    "magic": self.magic,
                    "comment": "TradeIdeaBot_BadFillClose",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                })
                return False, f"immediate_bad_fill@{fill}"
        return False, "not_pending_no_bad_position"

    def _sync_place_pending_order(self, symbol: str, direction: str, volume: float,
                                  entry_price: float, bid: float, ask: float,
                                  sl: float, tp: float, tick_size: float = 0.01) -> Optional[Any]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None

        plan = self.plan_pending_entry(
            direction, entry_price, bid, ask, tick_size=tick_size
        )
        if plan.mt5_order_type is None:
            logger.error(f"Invalid direction: {direction}")
            return None

        if plan.would_fill_immediately:
            logger.warning(
                f"Deferring pending {direction} {symbol} @ {entry_price}: {plan.defer_reason}"
            )
            return None

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": plan.mt5_order_type,
            "price": entry_price,
            "sl": sl,
            "tp": tp,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "TradeIdeaBot_Pending",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        logger.info(
            f"Placing {plan.kind.value} {direction} {symbol} @ {entry_price} "
            f"(bid={bid}, ask={ask})"
        )

        for attempt in range(1, self.max_order_retries + 1):
            try:
                result = mt5.order_send(request)
                if result is None:
                    continue
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    order_ticket = getattr(result, "order", 0)
                    ok, reason = self._sync_verify_pending_resting(
                        symbol, order_ticket, direction, entry_price, volume, tick_size
                    )
                    if not ok:
                        logger.error(
                            f"Pending {direction} {symbol} @ {entry_price} failed resting "
                            f"check: {reason}"
                        )
                        if order_ticket:
                            self._sync_cancel_pending_order(order_ticket)
                        return None
                    self._mark_success()
                    return result
                logger.warning(
                    f"Pending order failed attempt {attempt}/{self.max_order_retries}: "
                    f"retcode={result.retcode}"
                )
                if attempt < self.max_order_retries:
                    time.sleep(0.5 * attempt)
            except Exception as e:
                logger.error(f"place_pending_order exception (attempt {attempt}): {e}")
                if attempt < self.max_order_retries:
                    time.sleep(0.5 * attempt)
        return None

    async def place_pending_order(self, symbol: str, direction: str, volume: float,
                                  entry_price: float, bid: float, ask: float,
                                  sl: float, tp: float, tick_size: float = 0.01) -> Optional[Any]:
        """Place a pending Limit/Stop order. Returns MT5 result or None."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._sync_place_pending_order, symbol, direction, volume,
            entry_price, bid, ask, sl, tp, tick_size
        )

    def _sync_cancel_pending_order(self, ticket: int) -> Optional[Any]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }
        try:
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self._mark_success()
                return result
        except Exception as e:
            logger.error(f"cancel_pending_order exception: {e}")
        return None

    async def cancel_pending_order(self, ticket: int) -> Optional[Any]:
        """Cancel an unfilled pending order by ticket."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_cancel_pending_order, ticket)

    def _sync_get_orders(self, symbol: str = None) -> Optional[list]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None
        try:
            if symbol:
                orders = mt5.orders_get(symbol=symbol)
            else:
                orders = mt5.orders_get()
            if orders is not None:
                self._mark_success()
                return [o for o in orders if o.magic == self.magic]
            return None
        except Exception as e:
            logger.error(f"get_orders error: {e}")
            return None

    async def get_orders(self, symbol: str = None) -> Optional[list]:
        """Get active pending orders, optionally filtered by symbol. Returns None on error."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_orders, symbol)

    def _sync_resolve_pending_order_outcome(
        self,
        symbol: str,
        order_ticket: int,
        direction: str,
        volume: float,
        entry_price: float,
        tick_size: float = 0.01,
    ) -> PendingOrderOutcome:
        mt5 = self._get_mt5()
        if mt5 is None:
            return PendingOrderOutcome(status="missing")

        orders = mt5.orders_get(symbol=symbol)
        positions = mt5.positions_get(symbol=symbol)
        ours = [p for p in (positions or []) if getattr(p, "magic", 0) == self.magic]
        filtered_orders = (
            [o for o in orders if getattr(o, "magic", 0) == self.magic]
            if orders else []
        )

        history_order = None
        try:
            hist = mt5.history_orders_get(ticket=order_ticket)
            if hist:
                history_order = hist[0]
                self._mark_success()
        except Exception as e:
            logger.debug(f"history_orders_get({order_ticket}): {e}")

        order_deals = None
        try:
            order_deals = mt5.history_deals_get(order=order_ticket)
            if order_deals:
                self._mark_success()
        except Exception as e:
            logger.debug(f"history_deals_get(order={order_ticket}): {e}")

        return resolve_pending_order_outcome(
            order_ticket=order_ticket,
            symbol=symbol,
            direction=direction,
            volume=volume,
            entry_price=entry_price,
            tick_size=tick_size,
            magic=self.magic,
            orders=filtered_orders,
            positions=ours,
            history_order=history_order,
            order_deals=list(order_deals) if order_deals else None,
        )

    async def resolve_pending_order_outcome(
        self,
        symbol: str,
        order_ticket: int,
        direction: str,
        volume: float,
        entry_price: float,
        tick_size: float = 0.01,
    ) -> PendingOrderOutcome:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._sync_resolve_pending_order_outcome,
            symbol,
            order_ticket,
            direction,
            volume,
            entry_price,
            tick_size,
        )

