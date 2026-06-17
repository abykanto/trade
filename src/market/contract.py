"""Broker contract specs and dollar ↔ price conversions (XAUUSD, FX, etc.)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.market.direction import Direction


# Documented defaults for offline tests / fallback when MT5 spec is unavailable.
# Runtime code should prefer SymbolContract.from_mt5_spec().
SYMBOL_CONTRACT_DEFAULTS: dict[str, dict[str, float]] = {
    "XAUUSD": {
        "tick_size": 0.01,
        "tick_value": 1.0,
        "lot_step": 0.01,
        "lot_min": 0.01,
        "lot_max": 100.0,
    },
    "EURUSD": {
        "tick_size": 0.00001,
        "tick_value": 1.0,
        "lot_step": 0.01,
        "lot_min": 0.01,
        "lot_max": 100.0,
    },
    "GBPUSD": {
        "tick_size": 0.00001,
        "tick_value": 1.0,
        "lot_step": 0.01,
        "lot_min": 0.01,
        "lot_max": 100.0,
    },
    "USDJPY": {
        "tick_size": 0.001,
        "tick_value": 1.0,
        "lot_step": 0.01,
        "lot_min": 0.01,
        "lot_max": 100.0,
    },
}


@dataclass(frozen=True)
class SymbolContract:
    """MT5 contract terms for one symbol.

    Dollar PnL uses the same formula for every symbol::

        ticks = price_move / tick_size
        pnl   = ticks * tick_value * volume

    Example (XAUUSD): 0.05 lots, $1.00 adverse move (tick_size 0.01) → 100 ticks
    × tick_value 1.0 × 0.05 = $5.00.
    """

    symbol: str
    tick_size: float
    tick_value: float
    lot_step: float = 0.01
    lot_min: float = 0.01
    lot_max: float = 100.0

    @classmethod
    def from_mt5_spec(cls, symbol: str, spec: Any) -> SymbolContract:
        defaults = SYMBOL_CONTRACT_DEFAULTS.get(symbol.upper(), {})
        return cls(
            symbol=symbol.upper(),
            tick_size=getattr(spec, "trade_tick_size", defaults.get("tick_size", 0.00001)),
            tick_value=getattr(spec, "trade_tick_value", defaults.get("tick_value", 1.0)),
            lot_step=getattr(spec, "volume_step", defaults.get("lot_step", 0.01)),
            lot_min=getattr(spec, "volume_min", defaults.get("lot_min", 0.01)),
            lot_max=getattr(spec, "volume_max", defaults.get("lot_max", 100.0)),
        )

    @classmethod
    def for_symbol(cls, symbol: str, **overrides: float) -> SymbolContract:
        """Build a contract from documented defaults (tests, simulations)."""
        base = dict(SYMBOL_CONTRACT_DEFAULTS.get(symbol.upper(), {
            "tick_size": 0.00001,
            "tick_value": 1.0,
            "lot_step": 0.01,
            "lot_min": 0.01,
            "lot_max": 100.0,
        }))
        base.update(overrides)
        return cls(symbol=symbol.upper(), **base)

    def price_tolerance(self, multiplier: float = 2.0) -> float:
        return self.tick_size * multiplier

    def dollars_per_price_unit(self, volume: float) -> float:
        """Dollar PnL per one unit of price movement at `volume` lots."""
        if volume <= 0 or self.tick_size <= 0 or self.tick_value <= 0:
            return 0.0
        return volume * (self.tick_value / self.tick_size)

    def price_distance_for_dollars(self, dollars: float, volume: float) -> float:
        dollar_per_unit = self.dollars_per_price_unit(volume)
        if dollar_per_unit <= 0:
            return 0.0
        return dollars / dollar_per_unit

    def price_move(self, direction: str | Direction, entry: float, exit_price: float) -> float:
        return Direction.parse(direction).favorable_move(entry, exit_price)

    def ticks_from_price_move(self, price_move: float) -> float:
        if self.tick_size == 0:
            return 0.0
        return price_move / self.tick_size

    def dollar_pnl(
        self,
        direction: str | Direction,
        entry: float,
        exit_price: float,
        volume: float,
    ) -> float:
        move = self.price_move(direction, entry, exit_price)
        if volume <= 0 or self.tick_value == 0:
            return 0.0
        return self.ticks_from_price_move(move) * self.tick_value * volume

    def lot_size_for_risk(self, risk_amount: float, entry: float, stop_loss: float) -> float:
        distance = abs(entry - stop_loss)
        if distance == 0 or self.tick_size == 0 or self.tick_value == 0:
            return self.lot_min
        points = distance / self.tick_size
        lot_size = risk_amount / (points * self.tick_value)
        lot_size = round(lot_size / self.lot_step) * self.lot_step
        return max(self.lot_min, min(lot_size, self.lot_max))

    def stop_matches(self, actual_sl: float, expected_sl: float, multiplier: float = 2.0) -> bool:
        if actual_sl == 0.0:
            return False
        return abs(actual_sl - expected_sl) <= self.price_tolerance(multiplier)
