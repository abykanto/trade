import logging
import os
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date, timezone

from src.core.models import TradeIdea, TradeState
from src.market import Direction, SymbolContract

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
            
        if not Direction.parse(direction).stop_side_valid(entry, hard_stop):
            logger.warning(
                f"PortfolioRiskManager: Rejected {symbol}. "
                f"Invalid stop side for {direction} (entry={entry}, stop={hard_stop})."
            )
            return False

        # --- 5-LOSS CIRCUIT BREAKER (truly consecutive from most recent) ---
        recent_completed = db.query(TradeIdea).filter(
            TradeIdea.state.in_([
                TradeState.TP_REACHED.value,
                TradeState.IDEA_INVALIDATED.value,
            ])
        ).order_by(TradeIdea.updated_at.desc()).limit(20).all()

        consecutive_losses = 0
        for idea in recent_completed:
            if idea.realized_pnl < 0:
                consecutive_losses += 1
            else:
                break
        if consecutive_losses >= 5:
            logger.error(
                "PortfolioRiskManager: SYSTEM HALTED. "
                f"{consecutive_losses} consecutive losses detected. Manual review required."
            )
            return False

        # --- MAX ACTIVE LIMITS ---
        active_symbol_q = db.query(TradeIdea).filter(
            TradeIdea.symbol == symbol,
            TradeIdea.state.in_([
                TradeState.WAITING_FOR_SETUP.value,
                TradeState.SUBMITTING_ORDER.value,
                TradeState.PENDING_ORDER_PLACED.value,
                TradeState.TRADE_OPEN.value,
                TradeState.WAITING_FOR_REENTRY.value,
            ])
        )
        if exclude_idea_id is not None:
            active_symbol_q = active_symbol_q.filter(TradeIdea.id != exclude_idea_id)
        active_for_symbol = active_symbol_q.count()
        
        if active_for_symbol > 0:
            logger.warning(f"PortfolioRiskManager: Rejected {symbol}. One active idea per symbol allowed.")
            return False

        total_active_q = db.query(TradeIdea).filter(
            TradeIdea.state.in_([
                TradeState.WAITING_FOR_SETUP.value,
                TradeState.SUBMITTING_ORDER.value,
                TradeState.PENDING_ORDER_PLACED.value,
                TradeState.TRADE_OPEN.value,
                TradeState.WAITING_FOR_REENTRY.value,
            ])
        )
        if exclude_idea_id is not None:
            total_active_q = total_active_q.filter(TradeIdea.id != exclude_idea_id)
        total_active = total_active_q.count()
        
        if total_active >= self.max_active_ideas:
            logger.warning(f"PortfolioRiskManager: Rejected {symbol}. Max active ideas reached ({self.max_active_ideas}).")
            return False

        # --- DAILY DRAWDOWN HALT (Equity Based) ---
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        
        total_loss_today_q = db.query(func.sum(TradeIdea.realized_pnl)).filter(
            TradeIdea.updated_at >= today,
            TradeIdea.realized_pnl < 0
        )
        total_loss_today = total_loss_today_q.scalar() or 0.0
        
        if account_equity is not None and account_equity > 0:
            daily_loss_limit = account_equity * self.daily_dd_pct
            if abs(total_loss_today) >= daily_loss_limit:
                logger.warning(f"PortfolioRiskManager: Daily Drawdown Halt! ({abs(total_loss_today)} >= {daily_loss_limit:.2f}). No new ideas accepted.")
                return False
                
            # Account risk check — use remaining budget per idea, not full max_idea_risk
            active_ideas = db.query(TradeIdea).filter(
                TradeIdea.state.in_([
                    TradeState.WAITING_FOR_SETUP.value,
                    TradeState.SUBMITTING_ORDER.value,
                    TradeState.PENDING_ORDER_PLACED.value,
                    TradeState.TRADE_OPEN.value,
                    TradeState.WAITING_FOR_REENTRY.value,
                ])
            )
            if exclude_idea_id is not None:
                active_ideas = active_ideas.filter(TradeIdea.id != exclude_idea_id)
            active_risk = sum(
                max(0.0, idea.max_idea_risk - idea.consumed_risk)
                for idea in active_ideas.all()
            )

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
    """Thin facade over SymbolContract — keeps existing call sites stable."""

    @staticmethod
    def _clamp_lot(volume: float, contract: SymbolContract) -> float:
        if contract.lot_step > 0:
            volume = round(volume / contract.lot_step) * contract.lot_step
        return max(contract.lot_min, min(volume, contract.lot_max))

    @staticmethod
    def resolve_volume(
        contract: SymbolContract,
        risk_amount: float,
        entry: float,
        stop: float,
        predefined_lot: float | None = None,
        max_lot_size: float | None = None,
    ) -> tuple[float, str]:
        """Pick entry volume: predefined idea/signal lot, else risk-based (capped)."""
        if predefined_lot is not None and predefined_lot > 0:
            risk_cap = contract.lot_size_for_risk(risk_amount, entry, stop)
            volume = min(predefined_lot, risk_cap)
            return (
                PositionSizingEngine._clamp_lot(volume, contract),
                "predefined",
            )
        volume = contract.lot_size_for_risk(risk_amount, entry, stop)
        if max_lot_size is not None and max_lot_size > 0:
            volume = min(volume, max_lot_size)
        return PositionSizingEngine._clamp_lot(volume, contract), "dynamic"

    @staticmethod
    def max_lot_size_from_env() -> float | None:
        raw = os.environ.get("MAX_LOT_SIZE")
        if raw is None or raw.strip() == "":
            return None
        return float(raw)

    @staticmethod
    def calculate_lot_size(
        risk_amount: float,
        entry_price: float,
        stop_loss: float,
        tick_value: float,
        tick_size: float,
        lot_step: float = 0.01,
        lot_min: float = 0.01,
        lot_max: float = 100.0,
        symbol: str = "",
    ) -> float:
        contract = SymbolContract(
            symbol=symbol,
            tick_size=tick_size,
            tick_value=tick_value,
            lot_step=lot_step,
            lot_min=lot_min,
            lot_max=lot_max,
        )
        return contract.lot_size_for_risk(risk_amount, entry_price, stop_loss)

    @staticmethod
    def calculate_dollar_pnl(
        direction: str,
        entry_price: float,
        exit_price: float,
        volume: float,
        tick_value: float,
        tick_size: float,
        symbol: str = "",
    ) -> float:
        contract = SymbolContract(
            symbol=symbol,
            tick_size=tick_size,
            tick_value=tick_value,
        )
        return contract.dollar_pnl(direction, entry_price, exit_price, volume)
