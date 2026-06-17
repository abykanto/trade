import asyncio
import logging
import time
from enum import Enum
from typing import Optional, Any

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

    def __init__(self, host="localhost", port=18812, magic=234000,
                 deviation=20, max_retries=3):
        self.host = host
        self.port = port
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

    def _sync_connect(self) -> bool:
        mt5 = self._get_mt5()
        if mt5 is None:
            return False
        try:
            if mt5.initialize():
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

        price = tick.bid if direction.upper() == "BUY" else tick.ask

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
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self._mark_success()
                return result
            logger.warning(f"Modify position {ticket} failed: {getattr(result, 'retcode', 'None')}")
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

    # ── Pending orders ────────────────────────────────────────────

    def expected_pending_order_type(
        self, direction: str, entry_price: float, bid: float, ask: float
    ) -> int | None:
        """Return the MT5 pending order type for entry given live bid/ask."""
        mt5 = self._get_mt5()
        if mt5 is None:
            return None
        if direction.upper() == "BUY":
            # Buy when Ask reaches entry: limit if market above entry, stop if below
            return mt5.ORDER_TYPE_BUY_LIMIT if ask > entry_price else mt5.ORDER_TYPE_BUY_STOP
        if direction.upper() == "SELL":
            return mt5.ORDER_TYPE_SELL_LIMIT if bid < entry_price else mt5.ORDER_TYPE_SELL_STOP
        return None

    def _sync_place_pending_order(self, symbol: str, direction: str, volume: float,
                                  entry_price: float, bid: float, ask: float,
                                  sl: float, tp: float) -> Optional[Any]:
        mt5 = self._get_mt5()
        if mt5 is None or self.connection_state == ConnectionState.DISCONNECTED:
            return None

        order_type = self.expected_pending_order_type(direction, entry_price, bid, ask)
        if order_type is None:
            logger.error(f"Invalid direction: {direction}")
            return None

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": entry_price,
            "sl": sl,
            "tp": tp,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "TradeIdeaBot_Pending",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        for attempt in range(1, self.max_order_retries + 1):
            try:
                result = mt5.order_send(request)
                if result is None:
                    continue
                if result.retcode == mt5.TRADE_RETCODE_DONE:
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
                                  sl: float, tp: float) -> Optional[Any]:
        """Place a pending Limit/Stop order. Returns MT5 result or None."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._sync_place_pending_order, symbol, direction, volume,
            entry_price, bid, ask, sl, tp
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

