"""Runtime trading configuration (config.json + environment overrides)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from src.market.chop import ChopRules

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"


@dataclass
class TradingConfig:
    """Runtime trading parameters loaded from config.json and env vars."""

    chop: ChopRules

    def chop_distance_for(self, symbol: str) -> float:
        return self.chop.distance_for(symbol)

    def chop_exit_price(self, symbol: str, entry: float, direction: str) -> float:
        return self.chop.exit_price(symbol, entry, direction)

    def chop_stop_breached(self, symbol: str, entry: float, direction: str, price: float) -> bool:
        return self.chop.breached(symbol, entry, direction, price)


def load_trading_config(path: Path | None = None) -> TradingConfig:
    path = path or CONFIG_PATH
    data: dict = {}
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

    chop_default = float(os.environ.get("CHOP_EXIT_DISTANCE", data.get("chop_exit_distance", 1.0)))
    per_symbol = dict(data.get("symbol_chop_exit_distance", {}))

    for key, value in os.environ.items():
        if key.startswith("CHOP_EXIT_DISTANCE_"):
            symbol = key.removeprefix("CHOP_EXIT_DISTANCE_")
            per_symbol[symbol] = float(value)

    return TradingConfig(chop=ChopRules.from_config_data(chop_default, per_symbol))
