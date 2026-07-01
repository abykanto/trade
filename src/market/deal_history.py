"""Parse MT5 deal history for close price and realized PnL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.market.order_outcome import DEAL_ENTRY_IN, DEAL_ENTRY_OUT, _deal_entry_flag

# MT5 ORDER_TYPE_* for pending orders (stable across builds)
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_SELL_STOP = 5

_BUY_PENDING_TYPES = frozenset({ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_BUY_STOP})
_SELL_PENDING_TYPES = frozenset({ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_SELL_STOP})


@dataclass(frozen=True)
class PositionCloseDetails:
    """Broker-reported close from deal history."""

    close_price: float
    profit: float
    commission: float = 0.0
    swap: float = 0.0

    @property
    def net_profit(self) -> float:
        return self.profit + self.commission + self.swap


def pending_order_direction(order_type: int | None) -> str | None:
    if order_type in _BUY_PENDING_TYPES:
        return "BUY"
    if order_type in _SELL_PENDING_TYPES:
        return "SELL"
    return None


def order_matches_attempt(
    order: Any,
    *,
    symbol: str,
    direction: str,
    entry_price: float,
    volume: float,
    tick_size: float = 0.01,
) -> bool:
    """True when a live MT5 pending order likely belongs to a DB attempt."""
    if getattr(order, "symbol", "") != symbol:
        return False

    order_dir = pending_order_direction(int(getattr(order, "type", -1)))
    if order_dir != direction.upper():
        return False

    order_vol = float(getattr(order, "volume_current", 0.0) or getattr(order, "volume", 0.0))
    if abs(order_vol - volume) > 0.0001:
        return False

    order_price = float(
        getattr(order, "price_open", 0.0) or getattr(order, "price", 0.0) or 0.0
    )
    tol = tick_size if tick_size > 0 else 0.00001
    if abs(order_price - entry_price) > tol * 2:
        return False
    return True


def close_details_from_deals(deals: list | None) -> PositionCloseDetails | None:
    """Summarize exit deals for a closed position."""
    if not deals:
        return None

    out_deals = [d for d in deals if _deal_entry_flag(d) == DEAL_ENTRY_OUT]
    if not out_deals:
        in_deals = [d for d in deals if _deal_entry_flag(d) == DEAL_ENTRY_IN]
        if not in_deals:
            return None
        last = max(in_deals, key=lambda d: getattr(d, "time", 0))
        price = float(getattr(last, "price", 0.0) or 0.0)
        if price <= 0:
            return None
        profit = float(getattr(last, "profit", 0.0) or 0.0)
        commission = float(getattr(last, "commission", 0.0) or 0.0)
        swap = float(getattr(last, "swap", 0.0) or 0.0)
        return PositionCloseDetails(
            close_price=price,
            profit=profit,
            commission=commission,
            swap=swap,
        )

    last = max(out_deals, key=lambda d: getattr(d, "time", 0))
    close_price = float(getattr(last, "price", 0.0) or 0.0)
    if close_price <= 0:
        return None

    profit = sum(float(getattr(d, "profit", 0.0) or 0.0) for d in out_deals)
    commission = sum(float(getattr(d, "commission", 0.0) or 0.0) for d in out_deals)
    swap = sum(float(getattr(d, "swap", 0.0) or 0.0) for d in out_deals)
    return PositionCloseDetails(
        close_price=close_price,
        profit=profit,
        commission=commission,
        swap=swap,
    )
