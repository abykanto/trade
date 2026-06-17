import logging

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
    def __init__(self, progressive_secure_percentage: float = 0.60,
                 target_net_profit: float = 0.10, safety_buffer: float = 0.20):
        self.progressive_secure_percentage = progressive_secure_percentage
        self.target_net_profit = target_net_profit
        self.safety_buffer = safety_buffer

    @staticmethod
    def dollars_to_price_distance(dollars: float, lot_size: float,
                                   tick_value: float, tick_size: float) -> float:
        """Convert a dollar amount into a price distance for the given contract."""
        if lot_size <= 0 or tick_value <= 0 or tick_size <= 0:
            logger.error("Invalid contract specs for dollar-to-price conversion.")
            return 0.0
        dollar_per_point = lot_size * (tick_value / tick_size)
        return dollars / dollar_per_point

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
        min_move_distance: float = 0.0005
    ) -> float | None:
        """
        Returns a new absolute stop loss price, or None if no move is required.
        """
        is_buy = direction == "BUY"

        floating_dollars = PositionSizingEngine.calculate_dollar_pnl(
            direction, attempt_entry, current_price, lot_size, tick_value, tick_size
        )
        if floating_dollars <= 0:
            return None

        target = self.target_net_profit

        # Recovery mode: idea net is below target — lock only what is needed for target net
        if idea_realized_pnl < target:
            needed = target - idea_realized_pnl
            if floating_dollars < needed:
                return None
            lock_price = self.dollars_to_price_distance(
                needed, lot_size, tick_value, tick_size
            )
            proposed_sl = attempt_entry + lock_price if is_buy else attempt_entry - lock_price
            if is_buy:
                if proposed_sl > current_hard_stop + min_move_distance:
                    return proposed_sl
            else:
                if proposed_sl < current_hard_stop - min_move_distance:
                    return proposed_sl
            return None

        # Idea at/above target — progressive trailing (chop consumed_risk in dollars)
        profit_points = (current_price - attempt_entry) if is_buy else (attempt_entry - current_price)
        consumed_risk_price = self.dollars_to_price_distance(
            consumed_risk, lot_size, tick_value, tick_size
        )

        if profit_points <= consumed_risk_price:
            return None

        min_lock_dollars = consumed_risk + self.target_net_profit + self.safety_buffer
        min_lock_price = self.dollars_to_price_distance(
            min_lock_dollars, lot_size, tick_value, tick_size
        )

        net_profit_points = profit_points - consumed_risk_price
        total_target_distance = abs(take_profit - attempt_entry)
        distance_to_tp = abs(take_profit - current_price)

        if total_target_distance > 0:
            progress_ratio = 1.0 - (distance_to_tp / total_target_distance)
            progress_ratio = max(0.0, min(1.0, progress_ratio))
        else:
            progress_ratio = 0.0

        dynamic_secure_pct = self.progressive_secure_percentage + \
            (progress_ratio * (1.0 - self.progressive_secure_percentage) * 0.5)

        progressive_protection = net_profit_points * dynamic_secure_pct
        progressive_lock_price = consumed_risk_price + progressive_protection
        lock_price = max(min_lock_price, progressive_lock_price)

        proposed_sl = attempt_entry + lock_price if is_buy else attempt_entry - lock_price

        if is_buy:
            if proposed_sl > current_hard_stop + min_move_distance:
                return proposed_sl
        else:
            if proposed_sl < current_hard_stop - min_move_distance:
                return proposed_sl

        return None
