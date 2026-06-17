import logging

logger = logging.getLogger(__name__)

class TrailingStopEngine:
    """
    Implements Whipsaw Recovery & Progressive Trailing logic.
    
    The trailing stop has TWO phases:
    
    Phase 1 (Recovery): The trade must first recover all consumed_risk (dollars lost
    in previous whipsaw attempts). Until the floating profit exceeds consumed_risk,
    no trailing occurs.
    
    Phase 2 (Progressive): Once recovery is achieved, the stop is progressively
    tightened to lock in consumed_risk + a growing portion of net profit.
    As price approaches TP, the secure percentage increases dynamically.
    
    All dollar amounts are converted to price distances using contract specs:
        price_distance = dollar_amount / (lot_size * tick_value / tick_size)
    """
    def __init__(self, progressive_secure_percentage: float = 0.60,
                 target_net_profit: float = 0.10, safety_buffer: float = 0.20):
        self.progressive_secure_percentage = progressive_secure_percentage
        self.target_net_profit = target_net_profit  # Minimum $ profit to secure above recovery
        self.safety_buffer = safety_buffer          # Additional $ buffer on top of recovery

    @staticmethod
    def dollars_to_price_distance(dollars: float, lot_size: float,
                                   tick_value: float, tick_size: float) -> float:
        """Convert a dollar amount into a price distance for the given contract.
        
        Formula: price_distance = dollars / (lot_size * (tick_value / tick_size))
        
        Example (EURUSD, 0.10 lot, tick_value=1.0, tick_size=0.00001):
            $3.50 → 3.50 / (0.10 * 100000) = 0.00035 (35 pips)
        """
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
        min_move_distance: float = 0.0005
    ) -> float | None:
        """
        Calculates if the stop loss should be moved.
        
        Returns the new absolute stop loss price, or None if no move is required.
        
        The stop will NOT move until the floating profit (in dollars) exceeds
        consumed_risk. Once it does, the stop locks in:
            consumed_risk + target_net_profit + safety_buffer  (minimum)
        and progressively tightens as the price approaches take_profit.
        """
        is_buy = direction == "BUY"

        # Floating profit in price points
        profit_points = (current_price - attempt_entry) if is_buy else (attempt_entry - current_price)

        if profit_points <= 0:
            return None

        # Convert consumed_risk (dollars) to price distance
        consumed_risk_price = self.dollars_to_price_distance(
            consumed_risk, lot_size, tick_value, tick_size
        )

        # Phase 1: Still recovering — don't trail yet
        if profit_points <= consumed_risk_price:
            return None

        # Phase 2: Recovery achieved. Calculate the lock amount.
        # Minimum lock = consumed_risk + target_net_profit + safety_buffer (all in $)
        min_lock_dollars = consumed_risk + self.target_net_profit + self.safety_buffer
        min_lock_price = self.dollars_to_price_distance(
            min_lock_dollars, lot_size, tick_value, tick_size
        )

        # Progressive lock: as price approaches TP, secure more of the net profit
        net_profit_points = profit_points - consumed_risk_price
        total_target_distance = abs(take_profit - attempt_entry)
        distance_to_tp = abs(take_profit - current_price)

        if total_target_distance > 0:
            progress_ratio = 1.0 - (distance_to_tp / total_target_distance)
            progress_ratio = max(0.0, min(1.0, progress_ratio))
        else:
            progress_ratio = 0.0

        # Dynamic secure percentage increases as we approach TP
        dynamic_secure_pct = self.progressive_secure_percentage + \
            (progress_ratio * (1.0 - self.progressive_secure_percentage) * 0.5)

        progressive_protection = net_profit_points * dynamic_secure_pct
        progressive_lock_price = consumed_risk_price + progressive_protection

        # Use whichever lock is larger: the minimum recovery lock or the progressive lock
        lock_price = max(min_lock_price, progressive_lock_price)

        # Calculate the proposed stop loss
        proposed_sl = attempt_entry + lock_price if is_buy else attempt_entry - lock_price

        # Never move the stop backwards, and only move if > min_move_distance
        if is_buy:
            if proposed_sl > current_hard_stop + min_move_distance:
                return proposed_sl
        else:
            if proposed_sl < current_hard_stop - min_move_distance:
                return proposed_sl

        return None
