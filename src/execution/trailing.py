import logging

from src.market import Direction, SymbolContract
from src.risk.portfolio import PositionSizingEngine

logger = logging.getLogger(__name__)


class TrailingStopEngine:
    """
    Implements Whipsaw Recovery & Progressive Trailing logic.

    When idea_realized_pnl is below target_net_profit (recovery mode), the stop
    locks exactly enough profit on the current attempt to bring idea net to target.

    When the idea is already at/above target, progressive trailing uses consumed_risk
    (chop losses in dollars) for optional further protection toward TP.
    """

    def __init__(
        self,
        progressive_secure_percentage: float = 0.60,
        target_net_profit: float = 0.10,
        safety_buffer: float = 0.20,
    ):
        self.progressive_secure_percentage = progressive_secure_percentage
        self.target_net_profit = target_net_profit
        self.safety_buffer = safety_buffer

    @staticmethod
    def dollars_to_price_distance(
        dollars: float,
        lot_size: float,
        contract: SymbolContract,
    ) -> float:
        return contract.price_distance_for_dollars(dollars, lot_size)

    def calculate_new_stop(
        self,
        direction: str,
        attempt_entry: float,
        current_price: float,
        current_hard_stop: float,
        consumed_risk: float,
        take_profit: float,
        lot_size: float,
        tick_value: float,
        tick_size: float,
        idea_realized_pnl: float = 0.0,
        min_move_distance: float = 0.0005,
        symbol: str = "",
    ) -> float | None:
        """Returns a new absolute stop loss price, or None if no move is required."""
        contract = SymbolContract(
            symbol=symbol,
            tick_size=tick_size,
            tick_value=tick_value,
        )
        trade_dir = Direction.parse(direction)

        floating_dollars = contract.dollar_pnl(
            direction, attempt_entry, current_price, lot_size
        )
        if floating_dollars <= 0:
            return None

        target = self.target_net_profit

        if idea_realized_pnl < target:
            needed = target - idea_realized_pnl
            if floating_dollars < needed:
                return None
            lock_price = self.dollars_to_price_distance(needed, lot_size, contract)
            proposed_sl = trade_dir.offset_stop_from_entry(attempt_entry, lock_price)
            if trade_dir.is_buy():
                if proposed_sl > current_hard_stop + min_move_distance:
                    return proposed_sl
            else:
                if proposed_sl < current_hard_stop - min_move_distance:
                    return proposed_sl
            return None

        profit_points = trade_dir.favorable_move(attempt_entry, current_price)
        consumed_risk_price = self.dollars_to_price_distance(
            consumed_risk, lot_size, contract
        )

        if profit_points <= consumed_risk_price:
            return None

        min_lock_dollars = consumed_risk + self.target_net_profit + self.safety_buffer
        min_lock_price = self.dollars_to_price_distance(
            min_lock_dollars, lot_size, contract
        )

        net_profit_points = profit_points - consumed_risk_price
        total_target_distance = abs(take_profit - attempt_entry)
        distance_to_tp = abs(take_profit - current_price)

        if total_target_distance > 0:
            progress_ratio = 1.0 - (distance_to_tp / total_target_distance)
            progress_ratio = max(0.0, min(1.0, progress_ratio))
        else:
            progress_ratio = 0.0

        dynamic_secure_pct = self.progressive_secure_percentage + (
            progress_ratio * (1.0 - self.progressive_secure_percentage) * 0.5
        )

        progressive_protection = net_profit_points * dynamic_secure_pct
        progressive_lock_price = consumed_risk_price + progressive_protection
        lock_price = max(min_lock_price, progressive_lock_price)

        proposed_sl = trade_dir.offset_stop_from_entry(attempt_entry, lock_price)

        if trade_dir.is_buy():
            if proposed_sl > current_hard_stop + min_move_distance:
                return proposed_sl
        else:
            if proposed_sl < current_hard_stop - min_move_distance:
                return proposed_sl

        return None
