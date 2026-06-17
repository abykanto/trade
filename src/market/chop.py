"""Per-symbol chop (whipsaw) exit distances and prices."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.market.direction import Direction


@dataclass
class ChopRules:
    """Working stop distance while an attempt is live (symbol-overridable)."""

    default_distance: float = 1.0
    per_symbol: dict[str, float] = field(default_factory=dict)

    def distance_for(self, symbol: str) -> float:
        return self.per_symbol.get(symbol.upper(), self.default_distance)

    def exit_price(self, symbol: str, entry: float, direction: str | Direction) -> float:
        distance = self.distance_for(symbol)
        return Direction.parse(direction).chop_exit_price(entry, distance)

    def breached(
        self,
        symbol: str,
        entry: float,
        direction: str | Direction,
        price: float,
    ) -> bool:
        chop = self.exit_price(symbol, entry, direction)
        return Direction.parse(direction).level_breached_at_or_beyond(chop, price)

    @classmethod
    def from_config_data(
        cls,
        default_distance: float,
        per_symbol: dict[str, float] | None = None,
    ) -> ChopRules:
        normalized = {k.upper(): float(v) for k, v in (per_symbol or {}).items()}
        return cls(default_distance=float(default_distance), per_symbol=normalized)
