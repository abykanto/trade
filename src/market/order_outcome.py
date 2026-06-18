"""Resolve what happened to a pending MT5 order (resting, filled, closed, cancelled)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.market.pending_entry import fill_price_violates_entry
from src.market.position import resolve_position_ticket

# MT5 ORDER_STATE_* / DEAL_ENTRY_* (stable across builds)
ORDER_STATE_CANCELED = 2
ORDER_STATE_FILLED = 4
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1


@dataclass(frozen=True)
class PendingOrderOutcome:
    """Result of looking up a pending order ticket in MT5."""

    status: str
    # pending | open | closed | cancelled | bad_fill | missing
    position_ticket: int | None = None
    fill_price: float | None = None
    close_price: float | None = None


def _deal_entry_flag(deal: Any) -> int | None:
    entry = getattr(deal, "entry", None)
    if entry is None:
        return None
    try:
        return int(entry)
    except (TypeError, ValueError):
        return None


def resolve_pending_order_outcome(
    *,
    order_ticket: int,
    symbol: str,
    direction: str,
    volume: float,
    entry_price: float,
    tick_size: float,
    magic: int,
    orders: list | None,
    positions: list | None,
    history_order: Any | None,
    order_deals: list | None,
) -> PendingOrderOutcome:
    """Classify pending order state using live book + history snapshots."""
    if orders:
        for o in orders:
            if getattr(o, "ticket", 0) == order_ticket:
                return PendingOrderOutcome(status="pending")

    ours = [p for p in (positions or []) if getattr(p, "magic", 0) == magic]
    pos_ticket = resolve_position_ticket(
        ours, order_ticket, direction=direction, volume=volume, entry_price=entry_price
    )
    if pos_ticket is not None:
        pos = next(p for p in ours if int(getattr(p, "ticket", 0)) == pos_ticket)
        fill = float(getattr(pos, "price_open", 0.0))
        if fill_price_violates_entry(direction, entry_price, fill, tick_size):
            return PendingOrderOutcome(
                status="bad_fill", position_ticket=pos_ticket, fill_price=fill
            )
        return PendingOrderOutcome(
            status="open", position_ticket=pos_ticket, fill_price=fill
        )

    if history_order is not None:
        state = int(getattr(history_order, "state", -1))
        if state == ORDER_STATE_CANCELED:
            return PendingOrderOutcome(status="cancelled")
        if state == ORDER_STATE_FILLED:
            outcome = _outcome_from_deals(
                order_deals, direction, entry_price, tick_size, positions=ours
            )
            if outcome is not None:
                return outcome

    if order_deals:
        outcome = _outcome_from_deals(
            order_deals, direction, entry_price, tick_size, positions=ours
        )
        if outcome is not None:
            return outcome

    return PendingOrderOutcome(status="missing")


def _outcome_from_deals(
    deals: list | None,
    direction: str,
    entry_price: float,
    tick_size: float,
    positions: list | None = None,
) -> PendingOrderOutcome | None:
    if not deals:
        return None
    in_deals = [d for d in deals if _deal_entry_flag(d) == DEAL_ENTRY_IN]
    if not in_deals:
        return None

    entry_deal = max(in_deals, key=lambda d: getattr(d, "time", 0))
    fill = float(getattr(entry_deal, "price", 0.0))
    if fill_price_violates_entry(direction, entry_price, fill, tick_size):
        pos_id = int(getattr(entry_deal, "position_id", 0) or 0)
        return PendingOrderOutcome(
            status="bad_fill", fill_price=fill, position_ticket=pos_id or None
        )

    pos_id = int(getattr(entry_deal, "position_id", 0) or 0)
    if positions and pos_id:
        for p in positions:
            if int(getattr(p, "ticket", 0)) == pos_id:
                return PendingOrderOutcome(
                    status="open", position_ticket=pos_id, fill_price=fill
                )
            if int(getattr(p, "identifier", 0)) == pos_id:
                ticket = int(getattr(p, "ticket", 0))
                return PendingOrderOutcome(
                    status="open", position_ticket=ticket, fill_price=fill
                )

    out_deals = [
        d for d in deals
        if _deal_entry_flag(d) == DEAL_ENTRY_OUT
        and int(getattr(d, "position_id", 0) or 0) == pos_id
    ]
    if out_deals:
        exit_deal = max(out_deals, key=lambda d: getattr(d, "time", 0))
        close = float(getattr(exit_deal, "price", 0.0))
        return PendingOrderOutcome(
            status="closed",
            position_ticket=pos_id or None,
            fill_price=fill,
            close_price=close,
        )

    if pos_id:
        return PendingOrderOutcome(
            status="open", position_ticket=pos_id, fill_price=fill
        )
    return PendingOrderOutcome(status="closed", fill_price=fill, close_price=fill)
