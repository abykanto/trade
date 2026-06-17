"""Trade direction helpers — shared by all symbols."""

from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @classmethod
    def parse(cls, value: str | Direction) -> Direction:
        if isinstance(value, Direction):
            return value
        normalized = value.strip().upper()
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(f"Invalid direction: {value!r}") from exc

    def is_buy(self) -> bool:
        return self is Direction.BUY

    def is_sell(self) -> bool:
        return self is Direction.SELL

    def stop_must_be_below_entry(self) -> bool:
        """True when a protective stop sits on the losing side below entry (BUY)."""
        return self.is_buy()

    def adverse_move(self, entry: float, price: float) -> float:
        """Signed price move against the position (positive = losing)."""
        if self.is_buy():
            return entry - price
        return price - entry

    def favorable_move(self, entry: float, price: float) -> float:
        """Signed price move in the trade's profit direction."""
        if self.is_buy():
            return price - entry
        return entry - price

    def stop_side_valid(self, entry: float, stop: float) -> bool:
        if self.is_buy():
            return stop < entry
        return stop > entry

    def level_breached_at_or_beyond(self, level: float, price: float) -> bool:
        """True when price has traded through `level` in the stop-loss direction."""
        if self.is_buy():
            return price <= level
        return price >= level

    def level_breached_at_or_beyond_tp(self, level: float, price: float) -> bool:
        """True when price has traded through `level` in the take-profit direction."""
        if self.is_buy():
            return price >= level
        return price <= level

    def chop_exit_price(self, entry: float, distance: float) -> float:
        if self.is_buy():
            return entry - distance
        return entry + distance

    def offset_stop_from_entry(self, entry: float, price_distance: float) -> float:
        """Absolute stop price locking `price_distance` in the favorable direction."""
        if self.is_buy():
            return entry + price_distance
        return entry - price_distance
