from datetime import datetime, time, timezone
import logging

logger = logging.getLogger(__name__)

class SessionManager:
    def __init__(self):
        self.sessions = {
            "TOKYO": (0, 9),
            "LONDON": (8, 16),
            "NY": (13, 21)
        }
        
        self.symbol_prime_sessions = {
            "EURUSD": ["LONDON", "NY"],
            "GBPUSD": ["LONDON", "NY"],
            "USDJPY": ["TOKYO", "LONDON", "NY"],
            "AUDUSD": ["TOKYO", "LONDON", "NY"],
            "NZDUSD": ["TOKYO", "LONDON", "NY"],
            "USDCAD": ["NY"],
            "USDCHF": ["LONDON", "NY"],
            "XAUUSD": ["LONDON", "NY"],
            "XAGUSD": ["LONDON", "NY"],
            "USOIL": ["LONDON", "NY"],
            "UKOIL": ["LONDON", "NY"],
            "NAS100": ["NY"],
            "US30": ["NY"],
            "GER40": ["LONDON"],
        }

    def is_in_prime_session(self, symbol: str) -> bool:
        now_utc = datetime.now(timezone.utc).time()
        current_hour = now_utc.hour
        
        prime_sessions = self.symbol_prime_sessions.get(symbol, ["LONDON", "NY"])
        
        for session_name in prime_sessions:
            start, end = self.sessions.get(session_name, (0, 24))
            if start <= current_hour < end:
                return True
                
        logger.debug(f"{symbol} is NOT in a prime session. Current UTC hour: {current_hour}")
        return False

class LiquidityFilter:
    def __init__(self, rollover_start: time = time(23, 55), rollover_end: time = time(1, 5)):
        self.rollover_start = rollover_start
        self.rollover_end = rollover_end

    def is_rollover_period(self) -> bool:
        now = datetime.now(timezone.utc).time()
        
        # Handle wrap-around midnight
        if self.rollover_start > self.rollover_end:
            if now >= self.rollover_start or now <= self.rollover_end:
                logger.warning("LIQUIDITY FILTER: Execution blocked due to rollover period.")
                return True
        else:
            if self.rollover_start <= now <= self.rollover_end:
                logger.warning("LIQUIDITY FILTER: Execution blocked due to rollover period.")
                return True
                
        return False
