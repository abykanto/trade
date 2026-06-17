"""Idea-level stop / TP breach and exit classification."""

from __future__ import annotations

from src.market.contract import SymbolContract
from src.market.direction import Direction


class TradeLevels:
    """Shared rules for hard SL, TP, and chop vs trailing classification."""

    @staticmethod
    def final_hard_sl_breached(
        direction: str | Direction, hard_stop: float, price: float
    ) -> bool:
        return Direction.parse(direction).level_breached_at_or_beyond(hard_stop, price)

    @staticmethod
    def take_profit_breached(
        direction: str | Direction, take_profit: float, price: float
    ) -> bool:
        return Direction.parse(direction).level_breached_at_or_beyond_tp(take_profit, price)

    @staticmethod
    def working_stop_breached(
        direction: str | Direction, stop: float, price: float
    ) -> bool:
        return Direction.parse(direction).level_breached_at_or_beyond(stop, price)

    @staticmethod
    def classify_stop_exit(
        direction: str | Direction,
        close_price: float,
        chop_sl: float,
        hard_stop: float,
        contract: SymbolContract,
    ) -> str:
        """Classify a broker-side stop fill as chop loss or trailing exit."""
        d = Direction.parse(direction)
        tol = contract.price_tolerance(multiplier=3.0)
        if d.is_buy():
            if close_price <= chop_sl + tol:
                return "CHOP_EXIT"
            if hard_stop > chop_sl + tol:
                return "TRAILING_STOP"
            return "CHOP_EXIT"
        if close_price >= chop_sl - tol:
            return "CHOP_EXIT"
        if hard_stop < chop_sl - tol:
            return "TRAILING_STOP"
        return "CHOP_EXIT"
