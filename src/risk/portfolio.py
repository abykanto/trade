import logging
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date, timezone

from src.core.models import TradeIdea, TradeState

logger = logging.getLogger(__name__)

class PortfolioRiskManager:
    def __init__(self, daily_dd_pct=0.03, max_active_ideas=10, max_account_risk_percent=5.0):
        # daily_dd_pct replaces the fixed daily_loss_limit. e.g., 0.03 = 3%
        self.daily_dd_pct = daily_dd_pct
        self.max_active_ideas = max_active_ideas
        self.max_account_risk_percent = max_account_risk_percent

    def can_accept_idea(self, db: Session, symbol: str, direction: str, hard_stop: float, entry: float, max_idea_risk: float, account_equity: float = None, exclude_idea_id: int = None) -> bool:
        # Basic validation
        if max_idea_risk <= 0:
            logger.warning(f"PortfolioRiskManager: Rejected {symbol}. Negative or zero risk.")
            return False
            
        if direction.upper() == "BUY" and hard_stop >= entry:
            logger.warning(f"PortfolioRiskManager: Rejected {symbol}. BUY stop loss must be below entry.")
            return False
            
        if direction.upper() == "SELL" and hard_stop <= entry:
            logger.warning(f"PortfolioRiskManager: Rejected {symbol}. SELL stop loss must be above entry.")
            return False

        # --- 5-LOSS CIRCUIT BREAKER ---
        # Fetch the last 5 finalized ideas (TP, SL, or Invalidated with realized pnl)
        last_five_completed = db.query(TradeIdea).filter(
            TradeIdea.state.in_([
                TradeState.TP_REACHED.value,
                TradeState.IDEA_INVALIDATED.value
            ])
        ).order_by(TradeIdea.updated_at.desc()).limit(5).all()

        if len(last_five_completed) == 5:
            # Check if all 5 resulted in a loss (realized_pnl < 0)
            all_losses = all(idea.realized_pnl < 0 for idea in last_five_completed)
            if all_losses:
                logger.error("PortfolioRiskManager: SYSTEM HALTED. 5 consecutive losses detected. Manual review required.")
                return False

        # --- MAX ACTIVE LIMITS ---
        active_symbol_q = db.query(TradeIdea).filter(
            TradeIdea.symbol == symbol,
            TradeIdea.state.in_([
                TradeState.WAITING_FOR_SETUP.value,
                TradeState.PENDING_ORDER_PLACED.value,
                TradeState.TRADE_OPEN.value,
                TradeState.WAITING_FOR_REENTRY.value
            ])
        )
        if exclude_idea_id is not None:
            active_symbol_q = active_symbol_q.filter(TradeIdea.id != exclude_idea_id)
        active_for_symbol = active_symbol_q.count()
        
        if active_for_symbol > 0:
            logger.warning(f"PortfolioRiskManager: Rejected {symbol}. One active idea per symbol allowed.")
            return False

        total_active = db.query(TradeIdea).filter(
            TradeIdea.state.in_([
                TradeState.WAITING_FOR_SETUP.value,
                TradeState.PENDING_ORDER_PLACED.value,
                TradeState.TRADE_OPEN.value,
                TradeState.WAITING_FOR_REENTRY.value
            ])
        ).count()
        
        if total_active >= self.max_active_ideas:
            logger.warning(f"PortfolioRiskManager: Rejected {symbol}. Max active ideas reached ({self.max_active_ideas}).")
            return False

        # --- DAILY DRAWDOWN HALT (Equity Based) ---
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        
        total_loss_today = db.query(func.sum(TradeIdea.realized_pnl)).filter(
            TradeIdea.updated_at >= today,
            TradeIdea.realized_pnl < 0
        ).scalar() or 0.0
        
        if account_equity is not None and account_equity > 0:
            daily_loss_limit = account_equity * self.daily_dd_pct
            if abs(total_loss_today) >= daily_loss_limit:
                logger.warning(f"PortfolioRiskManager: Daily Drawdown Halt! ({abs(total_loss_today)} >= {daily_loss_limit:.2f}). No new ideas accepted.")
                return False
                
            # Account risk check
            active_risk_q = db.query(func.sum(TradeIdea.max_idea_risk)).filter(
                TradeIdea.state.in_([
                    TradeState.WAITING_FOR_SETUP.value,
                    TradeState.PENDING_ORDER_PLACED.value,
                    TradeState.TRADE_OPEN.value,
                    TradeState.WAITING_FOR_REENTRY.value
                ])
            )
            if exclude_idea_id is not None:
                active_risk_q = active_risk_q.filter(TradeIdea.id != exclude_idea_id)
            active_risk = active_risk_q.scalar() or 0.0
            
            total_proposed_risk = active_risk + max_idea_risk
            risk_percent = (total_proposed_risk / account_equity) * 100
            
            if risk_percent > self.max_account_risk_percent:
                logger.warning(f"PortfolioRiskManager: Max account risk exceeded ({risk_percent:.2f}% > {self.max_account_risk_percent}%).")
                return False
        else:
            # Fallback if equity is not available
            logger.warning("PortfolioRiskManager: Account equity not provided. Unable to calculate % DD.")
            return False

        return True

class PositionSizingEngine:
    @staticmethod
    def calculate_lot_size(
        risk_amount: float, 
        entry_price: float, 
        stop_loss: float, 
        tick_value: float,
        tick_size: float,
        lot_step: float = 0.01,
        lot_min: float = 0.01,
        lot_max: float = 100.0
    ) -> float:
        distance = abs(entry_price - stop_loss)
        if distance == 0 or tick_size == 0 or tick_value == 0:
            return lot_min
            
        # Points of distance
        points = distance / tick_size
        
        # Risk = lot_size * points * tick_value
        lot_size = risk_amount / (points * tick_value)
        
        # Round to lot step
        lot_size = round(lot_size / lot_step) * lot_step
        
        # Clamp to min/max
        return max(lot_min, min(lot_size, lot_max))
