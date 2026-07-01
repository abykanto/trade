"""Bid/ask tick and which side to use for stops, targets, and closes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.market.direction import Direction


@dataclass(frozen=True)
class MarketTick:
    bid: float
    ask: float

    @classmethod
    def from_mt5(cls, tick: Any) -> MarketTick:
        return cls(
            bid=getattr(tick, "bid", 0.0),
            ask=getattr(tick, "ask", 0.0),
        )

    def is_valid(self) -> bool:
        return self.bid > 0.0 and self.ask > 0.0

    def stop_trigger_price(self, direction: str | Direction) -> float:
        """Worst-case price for stop / final-SL checks (ask for SELL, bid for BUY)."""
        d = Direction.parse(direction)
        return self.ask if d.is_sell() else self.bid

    def tp_trigger_price(self, direction: str | Direction) -> float:
        """Price for take-profit checks (bid for SELL, ask for BUY)."""
        d = Direction.parse(direction)
        return self.bid if d.is_sell() else self.ask

    def close_price(self, direction: str | Direction) -> float:
        """Market close price for an open position (opposite side of entry)."""
        d = Direction.parse(direction)
        return self.bid if d.is_buy() else self.ask

    def pre_entry_sl_price(self, direction: str | Direction) -> float:
        return self.stop_trigger_price(direction)

    def pre_entry_tp_price(self, direction: str | Direction) -> float:
        return self.tp_trigger_price(direction)

    def open_trade_sl_price(self, direction: str | Direction) -> float:
        return self.stop_trigger_price(direction)

    def open_trade_tp_price(self, direction: str | Direction) -> float:
        return self.tp_trigger_price(direction)

    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def in_price_range(self, low: float, high: float) -> bool:
        """True when mid price is within [low, high] (entry zone check)."""
        mid = self.mid()
        return low <= mid <= high
