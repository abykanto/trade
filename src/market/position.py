"""MT5 position ticket resolution and stop verification."""

from __future__ import annotations

from typing import Any

from src.market.contract import SymbolContract
from src.market.direction import Direction


def position_direction(position: Any) -> Direction | None:
    pos_type = getattr(position, "type", None)
    if pos_type in (0, "BUY", Direction.BUY):
        return Direction.BUY
    if pos_type in (1, "SELL", Direction.SELL):
        return Direction.SELL
    return None


def resolve_position_ticket(
    positions: list,
    order_ticket: int,
    direction: str | Direction | None = None,
    volume: float | None = None,
    entry_price: float | None = None,
) -> int | None:
    """Map a pending order ticket to the live position ticket, if filled."""
    for p in positions:
        if getattr(p, "identifier", 0) != order_ticket:
            continue
        pos_ticket = getattr(p, "ticket", None)
        vol = getattr(p, "volume", 0.0)
        if pos_ticket and vol > 0 and int(pos_ticket) != order_ticket:
            return int(pos_ticket)

    if direction is None or volume is None:
        return None

    wanted = Direction.parse(direction)
    candidates = []
    for p in positions:
        pos_dir = position_direction(p)
        if pos_dir != wanted:
            continue
        if abs(getattr(p, "volume", 0.0) - volume) > 0.0001:
            continue
        pos_ticket = getattr(p, "ticket", None)
        if pos_ticket and int(pos_ticket) == order_ticket:
            continue
        candidates.append(p)

    if not candidates:
        return None
    if entry_price is not None:
        candidates.sort(
            key=lambda p: abs(getattr(p, "price_open", 0.0) - entry_price)
        )
    pos_ticket = getattr(candidates[0], "ticket", None)
    vol = getattr(candidates[0], "volume", 0.0)
    if pos_ticket and vol > 0:
        return int(pos_ticket)
    return None


def position_stop_matches(
    position: Any, expected_sl: float, contract: SymbolContract
) -> bool:
    return contract.stop_matches(getattr(position, "sl", 0.0), expected_sl)
