"""Runtime trading configuration (config.json + environment overrides)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"


@dataclass
class TradingConfig:
    """Chop/whipsaw exit: close open trades when price moves against entry by this distance."""

    chop_exit_distance: float = 1.0
    symbol_chop_exit_distance: dict[str, float] = field(default_factory=dict)

    def chop_distance_for(self, symbol: str) -> float:
        return self.symbol_chop_exit_distance.get(symbol, self.chop_exit_distance)

    def chop_exit_price(self, symbol: str, entry: float, direction: str) -> float:
        """Working stop for an open attempt: entry − distance (BUY), entry + distance (SELL)."""
        distance = self.chop_distance_for(symbol)
        if direction.upper() == "BUY":
            return entry - distance
        return entry + distance

    def chop_stop_breached(self, symbol: str, entry: float, direction: str, price: float) -> bool:
        chop = self.chop_exit_price(symbol, entry, direction)
        if direction.upper() == "BUY":
            return price <= chop
        return price >= chop


def load_trading_config(path: Path | None = None) -> TradingConfig:
    path = path or CONFIG_PATH
    data: dict = {}
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

    chop = float(os.environ.get("CHOP_EXIT_DISTANCE", data.get("chop_exit_distance", 1.0)))
    per_symbol = dict(data.get("symbol_chop_exit_distance", {}))

    for key, value in os.environ.items():
        if key.startswith("CHOP_EXIT_DISTANCE_"):
            symbol = key.removeprefix("CHOP_EXIT_DISTANCE_")
            per_symbol[symbol] = float(value)

    return TradingConfig(chop_exit_distance=chop, symbol_chop_exit_distance=per_symbol)
