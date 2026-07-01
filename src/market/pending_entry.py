"""Pending entry order type selection — only rest orders that trigger at the signal entry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.market.direction import Direction


class PendingOrderKind(str, Enum):
    BUY_LIMIT = "BUY_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_LIMIT = "SELL_LIMIT"
    SELL_STOP = "SELL_STOP"


@dataclass(frozen=True)
class PendingEntryPlan:
    """How to place a pending order so it rests until price reaches ``entry``."""

    kind: PendingOrderKind
    entry: float
    bid: float
    ask: float
    mt5_order_type: int | None = None

    @property
    def can_place(self) -> bool:
        return self.mt5_order_type is not None and not self.would_fill_immediately

    @property
    def would_fill_immediately(self) -> bool:
        return pending_would_fill_immediately(
            self.kind, self.entry, self.bid, self.ask
        )

    @property
    def defer_reason(self) -> str:
        if self.would_fill_immediately:
            if entry_inside_spread(self.entry, self.bid, self.ask):
                return (
                    f"entry {self.entry} inside spread (bid={self.bid}, ask={self.ask}) "
                    f"— waiting for price to leave spread before resting at entry"
                )
            return (
                f"{self.kind.value} @ {self.entry} would execute at current "
                f"market (bid={self.bid}, ask={self.ask}) instead of resting"
            )
        return ""


def entry_inside_spread(
    entry: float, bid: float, ask: float, tick_size: float = 0.0
) -> bool:
    """True when entry sits inside (or on) the spread — cannot rest at exact price."""
    eps = tick_size if tick_size > 0 else 0.0
    return bid > 0 and ask > 0 and (bid - eps) <= entry <= (ask + eps)


def pending_would_fill_immediately(
    kind: PendingOrderKind,
    entry: float,
    bid: float,
    ask: float,
    tick_size: float = 0.0,
) -> bool:
    """True when MT5 would fill this pending order right away (not at a future touch of entry)."""
    if entry_inside_spread(entry, bid, ask, tick_size):
        return True

    eps = tick_size if tick_size > 0 else 0.0

    if kind == PendingOrderKind.BUY_LIMIT:
        # Buy limit fills when ask <= entry. Rest only when ask > entry.
        return ask <= entry + eps
    if kind == PendingOrderKind.BUY_STOP:
        # Buy stop fills when ask >= entry. Rest only when ask < entry.
        return ask >= entry - eps
    if kind == PendingOrderKind.SELL_LIMIT:
        # Sell limit fills when bid >= entry. Rest only when bid < entry.
        return bid >= entry - eps
    if kind == PendingOrderKind.SELL_STOP:
        # Sell stop fills when bid <= entry. Rest only when bid > entry.
        return bid <= entry + eps
    return True


def select_pending_order_kind(
    direction: str | Direction,
    entry: float,
    bid: float,
    ask: float,
    tick_size: float = 0.0,
) -> PendingOrderKind:
    """Pick limit vs stop so the order rests until price trades at ``entry``.

  BUY above market → BUY_STOP (trigger when ask rises to entry).
  BUY below market → BUY_LIMIT (trigger when ask falls to entry).
  SELL above market → SELL_LIMIT (trigger when bid rises to entry).
  SELL below market → SELL_STOP (trigger when bid falls to entry).
    """
    d = Direction.parse(direction)
    eps = tick_size if tick_size > 0 else 0.0

    if d.is_buy():
        if entry > ask + eps:
            return PendingOrderKind.BUY_STOP
        if entry < ask - eps:
            return PendingOrderKind.BUY_LIMIT
        # Inside spread — kind is nominal; caller must defer via would_fill_immediately
        return PendingOrderKind.BUY_LIMIT

    if entry < bid - eps:
        return PendingOrderKind.SELL_STOP
    if entry > bid + eps:
        return PendingOrderKind.SELL_LIMIT
    # Inside spread — nominal kind; caller defers
    return PendingOrderKind.SELL_LIMIT


def entry_market_side(
    direction: str | Direction,
    entry: float,
    bid: float,
    ask: float,
    tick_size: float = 0.0,
) -> str:
    """Describe where live price sits vs entry (drives limit vs stop on re-entry)."""
    if entry_inside_spread(entry, bid, ask, tick_size):
        return "inside_spread"
    d = Direction.parse(direction)
    eps = tick_size if tick_size > 0 else 0.0
    if d.is_buy():
        if entry > ask + eps:
            return "market_below_entry"
        if entry < ask - eps:
            return "market_above_entry"
    else:
        if bid > entry + eps:
            return "market_above_entry"
        if bid < entry - eps:
            return "market_below_entry"
    return "inside_spread"


def plan_pending_entry(
    direction: str | Direction,
    entry: float,
    bid: float,
    ask: float,
    tick_size: float = 0.0,
    mt5_type_map: dict[PendingOrderKind, int] | None = None,
) -> PendingEntryPlan:
    kind = select_pending_order_kind(direction, entry, bid, ask, tick_size)
    mt5_type = None
    if mt5_type_map is not None:
        mt5_type = mt5_type_map.get(kind)
    return PendingEntryPlan(
        kind=kind,
        entry=entry,
        bid=bid,
        ask=ask,
        mt5_order_type=mt5_type,
    )


def ready_for_initial_entry_placement(
    direction: str | Direction,
    entry: float,
    bid: float,
    ask: float,
    zone_low: float,
    zone_high: float,
    tick_size: float = 0.0,
) -> bool:
    """Whether the first pending for an idea may be armed.

    Proceed when price is inside the entry zone, or when a resting pending at
    ``entry`` is possible (even if mid is outside the zone).  Only hold back
    when price is outside the zone *and* a pending would fill immediately.
    """
    mid = (bid + ask) / 2.0
    if zone_low <= mid <= zone_high:
        return True
    kind = select_pending_order_kind(direction, entry, bid, ask, tick_size)
    return not pending_would_fill_immediately(kind, entry, bid, ask, tick_size)


def fill_price_violates_entry(
    direction: str | Direction,
    entry: float,
    fill_price: float,
    tick_size: float,
    max_slippage_ticks: float = 3.0,
) -> bool:
    """True when a fill price is too far from the requested entry (bad immediate fill)."""
    if tick_size <= 0:
        tick_size = 0.01
    tol = tick_size * max_slippage_ticks
    d = Direction.parse(direction)
    if d.is_buy():
        # Filled far below entry → bought on a dump, not on a touch of entry
        if fill_price < entry - tol:
            return True
        # Filled far above entry → unexpected slippage
        if fill_price > entry + tol * 5:
            return True
        return False
    if fill_price > entry + tol:
        return True
    if fill_price < entry - tol * 5:
        return True
    return False
