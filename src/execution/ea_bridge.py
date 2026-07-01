"""Execution bridge that delegates broker operations to the MQL5 EA over TCP.

The legacy Python mt5linux bridge (``MT5Bridge``) remains available for reference
and for ``EXECUTION_BACKEND=mt5linux``.  Use ``EXECUTION_BACKEND=ea`` to route
orders through the EA instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from types import SimpleNamespace
from typing import Any, Optional

from src.execution.bridge import ConnectionState
from src.execution.ea_server import EAServer
from src.execution.protocol import (
    CMD_CANCEL_ORDER,
    CMD_CLOSE_POSITION,
    CMD_GET_ACCOUNT_INFO,
    CMD_GET_CLOSE_DETAILS,
    CMD_GET_ORDER_HISTORY,
    CMD_GET_ORDERS,
    CMD_GET_POSITION,
    CMD_GET_POSITIONS,
    CMD_GET_SYMBOL_SPEC,
    CMD_GET_TICK,
    CMD_MODIFY_POSITION,
    CMD_PLACE_PENDING,
    CMD_PING,
    EARequest,
)
from src.market import SymbolContract, position_stop_matches
from src.market.deal_history import PositionCloseDetails
from src.market.order_outcome import PendingOrderOutcome, resolve_pending_order_outcome
from src.market.pending_entry import (
    PendingOrderKind,
    plan_pending_entry,
)

logger = logging.getLogger(__name__)

_MT5_PENDING_TYPE_MAP = {
    PendingOrderKind.BUY_LIMIT: 2,
    PendingOrderKind.BUY_STOP: 4,
    PendingOrderKind.SELL_LIMIT: 3,
    PendingOrderKind.SELL_STOP: 5,
}

TRADE_RETCODE_DONE = 10009


def _ns(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


class EABridge:
    """Drop-in execution backend that talks to ``mql5/Experts/TradeIdeaExecutor.mq5``."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        magic: int = 234000,
        deviation: int = 20,
        max_retries: int = 3,
    ):
        self.magic = magic
        self.deviation = deviation
        self.max_order_retries = max_retries
        self.connection_state = ConnectionState.DISCONNECTED
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._last_successful_call = 0.0
        self._degraded_timeout = 30.0

        ea_host = host or os.environ.get("EA_SERVER_HOST", "0.0.0.0")
        ea_port = port if port is not None else int(os.environ.get("EA_SERVER_PORT", "19520"))
        self._server = EAServer(host=ea_host, port=ea_port)

    # ── Connection ────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            await self._server.start()
            logger.info(
                "Waiting for MQL5 EA on %s:%s (attach TradeIdeaExecutor, enable Algo Trading)",
                self._server.host,
                self._server.port,
            )
            if not await self._server.wait_for_ea():
                logger.error(
                    "EA did not connect within timeout. "
                    "Attach Experts/TradeIdeaExecutor to a chart in MT5."
                )
                self.connection_state = ConnectionState.DISCONNECTED
                return False
            if not await self._server.ping():
                logger.error("EA connected but PING failed")
                self.connection_state = ConnectionState.DISCONNECTED
                return False
            self.connection_state = ConnectionState.ACTIVE
            self._last_successful_call = time.monotonic()
            self._reconnect_attempts = 0
            logger.info("Connected to MQL5 EA executor")
            return True
        except Exception as exc:
            logger.error("EA bridge connect failed: %s", exc)
            self.connection_state = ConnectionState.DISCONNECTED
            return False

    async def reconnect(self) -> bool:
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.error("Max EA reconnection attempts reached.")
            return False
        delay = min(2 ** self._reconnect_attempts, 60)
        self._reconnect_attempts += 1
        logger.info("EA reconnect in %ss (attempt %s)...", delay, self._reconnect_attempts)
        await asyncio.sleep(delay)
        return await self.connect()

    async def shutdown(self) -> None:
        await self._server.stop()
        self.connection_state = ConnectionState.DISCONNECTED
        logger.info("EA bridge stopped.")

    def set_trade_event_handler(self, handler) -> None:
        """Register async callback for EA TRADE_EVENT push messages."""
        self._server.set_trade_event_handler(handler)

    @property
    def ea_server(self) -> EAServer:
        return self._server

    def _mark_success(self) -> None:
        self._last_successful_call = time.monotonic()
        if self.connection_state == ConnectionState.DEGRADED:
            self.connection_state = ConnectionState.ACTIVE

    def _check_connection(self) -> bool:
        if self.connection_state == ConnectionState.DISCONNECTED:
            return False
        if not self._server.is_connected:
            self.connection_state = ConnectionState.DISCONNECTED
            return False
        elapsed = time.monotonic() - self._last_successful_call
        if elapsed > self._degraded_timeout:
            if self.connection_state != ConnectionState.DEGRADED:
                logger.warning("EA connection DEGRADED — no successful call recently.")
                self.connection_state = ConnectionState.DEGRADED
        return True

    async def _cmd(self, cmd: str, **params) -> tuple[bool, dict]:
        if not self._check_connection() and cmd != CMD_PING:
            return False, {"error": "EA not connected"}
        rsp = await self._server.request(EARequest(cmd=cmd, params=params))
        if rsp.ok:
            self._mark_success()
            return True, rsp.data
        return False, {"error": rsp.error, "retcode": rsp.retcode, **rsp.data}

    # ── Market data ───────────────────────────────────────────────────────

    async def get_tick(self, symbol: str) -> Optional[Any]:
        ok, data = await self._cmd(CMD_GET_TICK, symbol=symbol)
        if not ok:
            return None
        return _ns(bid=float(data.get("bid", 0.0)), ask=float(data.get("ask", 0.0)))

    async def get_symbol_spec(self, symbol: str) -> Optional[Any]:
        ok, data = await self._cmd(CMD_GET_SYMBOL_SPEC, symbol=symbol)
        if not ok:
            return None
        return _ns(
            trade_tick_size=float(data.get("trade_tick_size", 0.01)),
            trade_tick_value=float(data.get("trade_tick_value", 1.0)),
            volume_min=float(data.get("volume_min", 0.01)),
            volume_max=float(data.get("volume_max", 100.0)),
            volume_step=float(data.get("volume_step", 0.01)),
            digits=int(data.get("digits", 2)),
        )

    async def get_account_info(self) -> Optional[Any]:
        ok, data = await self._cmd(CMD_GET_ACCOUNT_INFO)
        if not ok:
            return None
        return _ns(
            balance=float(data.get("balance", 0.0)),
            equity=float(data.get("equity", 0.0)),
            margin=float(data.get("margin", 0.0)),
            login=int(data.get("login", 0)),
            leverage=int(data.get("leverage", 0)),
        )

    # ── Pending entry planning (pure Python, same as MT5Bridge) ───────────

    def plan_pending_entry(
        self, direction: str, entry_price: float, bid: float, ask: float,
        tick_size: float = 0.01,
    ):
        return plan_pending_entry(
            direction, entry_price, bid, ask, tick_size=tick_size,
            mt5_type_map=_MT5_PENDING_TYPE_MAP,
        )

    def expected_pending_order_type(
        self, direction: str, entry_price: float, bid: float, ask: float,
        tick_size: float = 0.01,
    ) -> int | None:
        plan = self.plan_pending_entry(direction, entry_price, bid, ask, tick_size=tick_size)
        return plan.mt5_order_type

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

    # ── Orders & positions ────────────────────────────────────────────────

    async def place_pending_order(
        self,
        symbol: str,
        direction: str,
        volume: float,
        entry_price: float,
        bid: float,
        ask: float,
        sl: float,
        tp: float,
        tick_size: float = 0.01,
    ) -> Optional[Any]:
        plan = self.plan_pending_entry(direction, entry_price, bid, ask, tick_size=tick_size)
        if plan.mt5_order_type is None:
            logger.error("Invalid direction for pending order: %s", direction)
            return None
        if plan.would_fill_immediately:
            logger.warning(
                "Deferring pending %s %s @ %s: %s",
                direction, symbol, entry_price, plan.defer_reason,
            )
            return None

        for attempt in range(1, self.max_order_retries + 1):
            ok, data = await self._cmd(
                CMD_PLACE_PENDING,
                symbol=symbol,
                direction=direction.upper(),
                volume=volume,
                entry=entry_price,
                sl=sl,
                tp=tp,
                bid=bid,
                ask=ask,
                order_type=int(plan.mt5_order_type),
                order_kind=plan.kind.value,
                tick_size=tick_size,
                deviation=self.deviation,
                magic=self.magic,
            )
            if ok:
                return _ns(
                    retcode=int(data.get("retcode", TRADE_RETCODE_DONE)),
                    order=int(data.get("order", 0)),
                    comment=str(data.get("comment", "")),
                )
            retcode = data.get("retcode")
            if retcode == 10027:
                logger.error("EA blocked: enable Algo Trading in MT5 (retcode=10027)")
                return None
            logger.warning(
                "EA place_pending attempt %s/%s failed: %s",
                attempt, self.max_order_retries, data.get("error"),
            )
            if attempt < self.max_order_retries:
                await asyncio.sleep(0.5 * attempt)
        return None

    async def cancel_pending_order(self, ticket: int) -> Optional[Any]:
        ok, data = await self._cmd(CMD_CANCEL_ORDER, ticket=int(ticket), magic=self.magic)
        if not ok:
            return None
        return _ns(retcode=int(data.get("retcode", TRADE_RETCODE_DONE)))

    async def modify_position(
        self, ticket: int, symbol: str, sl: float, tp: float
    ) -> Optional[Any]:
        ok, data = await self._cmd(
            CMD_MODIFY_POSITION,
            ticket=int(ticket),
            symbol=symbol,
            sl=sl,
            tp=tp,
            magic=self.magic,
        )
        if not ok:
            return None
        return _ns(retcode=int(data.get("retcode", TRADE_RETCODE_DONE)))

    async def close_position(
        self, ticket: int, symbol: str, direction: str, volume: float
    ) -> Optional[Any]:
        ok, data = await self._cmd(
            CMD_CLOSE_POSITION,
            ticket=int(ticket),
            symbol=symbol,
            direction=direction.upper(),
            volume=volume,
            deviation=self.deviation,
            magic=self.magic,
        )
        if not ok:
            return None
        return _ns(
            retcode=int(data.get("retcode", TRADE_RETCODE_DONE)),
            price=float(data.get("price", 0.0)),
        )

    def _map_position(self, raw: dict) -> SimpleNamespace:
        return _ns(
            ticket=int(raw.get("ticket", 0)),
            identifier=int(raw.get("identifier", raw.get("ticket", 0))),
            magic=int(raw.get("magic", self.magic)),
            volume=float(raw.get("volume", 0.0)),
            price_open=float(raw.get("price_open", 0.0)),
            sl=float(raw.get("sl", 0.0)),
            tp=float(raw.get("tp", 0.0)),
            type=int(raw.get("type", 0)),
            symbol=str(raw.get("symbol", "")),
        )

    def _map_deal(self, raw: dict) -> SimpleNamespace:
        return _ns(
            ticket=int(raw.get("ticket", 0)),
            price=float(raw.get("price", 0.0)),
            entry=int(raw.get("entry", 0)),
            position_id=int(raw.get("position_id", 0)),
            time=int(raw.get("time", 0)),
        )

    def _map_history_order(self, raw: dict | None, order_ticket: int) -> SimpleNamespace | None:
        if not raw:
            return None
        state = raw.get("state")
        if state is None:
            return None
        return _ns(
            ticket=order_ticket,
            state=int(state),
            comment=str(raw.get("comment", "") or ""),
            magic=int(raw.get("magic", self.magic) or 0),
        )

    def _map_order(self, raw: dict) -> SimpleNamespace:
        return _ns(
            ticket=int(raw.get("ticket", 0)),
            magic=int(raw.get("magic", self.magic)),
            volume=float(raw.get("volume", raw.get("volume_current", 0.0))),
            volume_current=float(raw.get("volume_current", raw.get("volume", 0.0))),
            price_open=float(raw.get("price_open", raw.get("price", 0.0))),
            price=float(raw.get("price", raw.get("price_open", 0.0))),
            type=int(raw.get("type", 0)),
            symbol=str(raw.get("symbol", "")),
        )

    async def get_positions(self, symbol: str | None = None) -> Optional[list]:
        params: dict[str, Any] = {"magic": self.magic}
        if symbol:
            params["symbol"] = symbol
        ok, data = await self._cmd(CMD_GET_POSITIONS, **params)
        if not ok:
            return None
        return [self._map_position(p) for p in data.get("positions", [])]

    async def get_position(self, ticket: int) -> Optional[Any]:
        ok, data = await self._cmd(CMD_GET_POSITION, ticket=int(ticket), magic=self.magic)
        if not ok or not data.get("position"):
            return None
        return self._map_position(data["position"])

    async def get_orders(self, symbol: str | None = None) -> Optional[list]:
        params: dict[str, Any] = {"magic": self.magic}
        if symbol:
            params["symbol"] = symbol
        ok, data = await self._cmd(CMD_GET_ORDERS, **params)
        if not ok:
            return None
        return [self._map_order(o) for o in data.get("orders", [])]

    async def get_position_close_details(
        self, position_ticket: int
    ) -> Optional[PositionCloseDetails]:
        ok, data = await self._cmd(CMD_GET_CLOSE_DETAILS, ticket=int(position_ticket))
        if not ok:
            return None
        return PositionCloseDetails(
            close_price=float(data.get("close_price", 0.0)),
            profit=float(data.get("profit", 0.0)),
            commission=float(data.get("commission", 0.0)),
            swap=float(data.get("swap", 0.0)),
        )

    async def get_position_close_price(self, position_ticket: int) -> Optional[float]:
        details = await self.get_position_close_details(position_ticket)
        return details.close_price if details else None

    async def resolve_pending_order_outcome(
        self,
        symbol: str,
        order_ticket: int,
        direction: str,
        volume: float,
        entry_price: float,
        tick_size: float = 0.01,
    ) -> PendingOrderOutcome:
        """Resolve pending outcome in Python using EA-fetched book + history snapshots."""
        orders = await self.get_orders(symbol)
        positions = await self.get_positions(symbol)
        if orders is None or positions is None:
            return PendingOrderOutcome(status="missing")

        ok, data = await self._cmd(CMD_GET_ORDER_HISTORY, order_ticket=int(order_ticket))
        history_order = None
        order_deals = None
        if ok:
            history_order = self._map_history_order(
                data.get("history_order"), order_ticket
            )
            raw_deals = data.get("deals")
            if raw_deals is not None:
                order_deals = [self._map_deal(d) for d in raw_deals]

        return resolve_pending_order_outcome(
            order_ticket=order_ticket,
            symbol=symbol,
            direction=direction,
            volume=volume,
            entry_price=entry_price,
            tick_size=tick_size,
            magic=self.magic,
            orders=orders,
            positions=positions,
            history_order=history_order,
            order_deals=order_deals,
        )

    # Unused by TradeManager but kept for API parity with MT5Bridge
    async def place_order(
        self, symbol: str, direction: str, volume: float,
        price: float, sl: float, tp: float,
    ) -> Optional[Any]:
        logger.warning("place_order market path not used; use place_pending_order via EA")
        return None
