import pytest
from datetime import datetime, timezone
from src.execution.trailing import TrailingStopEngine
from src.risk.portfolio import PortfolioRiskManager, PositionSizingEngine
from src.core.models import TradeIdea, TradeState
from src.core.database import init_db, get_session_local, reset_db

@pytest.fixture(autouse=True)
def setup_db():
    reset_db()
    engine = init_db("sqlite:///:memory:")
    yield engine
    reset_db()

@pytest.fixture
def db_session(setup_db):
    SessionLocal = get_session_local(setup_db)
    session = SessionLocal()
    yield session
    session.close()

def test_trailing_stop_whipsaw_recovery():
    """
    User's exact example:
      Original Entry = 1000.50, SL = 995, TP = 1012
      Attempt #1 = -1.20, #2 = -0.80, #3 = -0.60, #4 = -0.90
      Consumed Risk = $3.50
      Target Net Profit = $0.10, Safety Buffer = $0.20
      Trailing Lock = $3.80
      
    Contract specs (simplified):
      lot_size = 1.0, tick_value = 1.0, tick_size = 0.01
      So $1.00 = 0.01 price distance (1 tick = $1 per lot)
    """
    engine = TrailingStopEngine(
        progressive_secure_percentage=0.6,
        target_net_profit=0.10,
        safety_buffer=0.20
    )
    
    # Contract: $1 per tick, tick = 0.01, 1 lot
    lot_size = 1.0
    tick_value = 1.0
    tick_size = 0.01
    
    # Phase 1: Price hasn't recovered consumed_risk yet — NO trailing
    # consumed_risk = $3.50 → price distance = 3.50 / (1.0 * (1.0/0.01)) = 3.50 / 100 = 0.035
    # profit_points at 1000.53 = 0.03 < 0.035 → None
    new_sl = engine.calculate_new_stop(
        direction="BUY", attempt_entry=1000.50, current_price=1000.53,
        current_hard_stop=995.0, consumed_risk=3.50, take_profit=1012.0,
        lot_size=lot_size, tick_value=tick_value, tick_size=tick_size
    )
    assert new_sl is None, "Should NOT trail while still recovering consumed risk"
    
    # Phase 2: Price has recovered and is approaching TP
    # current_price = 1004.50 → profit_points = 4.00
    # consumed_risk_price = 0.035
    # profit_points (4.00) > consumed_risk_price (0.035) → recovery achieved!
    # min_lock_dollars = 3.50 + 0.10 + 0.20 = $3.80
    # min_lock_price = 3.80 / 100 = 0.038
    # proposed_sl = 1000.50 + 0.038 = 1000.538
    # But progressive lock will be larger since profit is much bigger...
    new_sl = engine.calculate_new_stop(
        direction="BUY", attempt_entry=1000.50, current_price=1004.50,
        current_hard_stop=995.0, consumed_risk=3.50, take_profit=1012.0,
        lot_size=lot_size, tick_value=tick_value, tick_size=tick_size
    )
    assert new_sl is not None, "Should trail after recovery"
    assert new_sl > 1000.50, "Stop must be above entry to lock in recovery"

def test_trailing_stop_no_consumed_risk():
    """When consumed_risk = 0 (first attempt, no whipsaw), trailing should
    work purely on progressive protection from the start."""
    engine = TrailingStopEngine(progressive_secure_percentage=0.6)
    
    new_sl = engine.calculate_new_stop(
        direction="BUY", attempt_entry=1.1000, current_price=1.1050,
        current_hard_stop=1.0950, consumed_risk=0.0, take_profit=1.1200,
        lot_size=0.10, tick_value=1.0, tick_size=0.00001
    )
    assert new_sl is not None, "Should trail immediately when no consumed risk"
    assert new_sl > 1.1000, "Stop must lock in some profit"

def test_trailing_stop_never_moves_backward():
    """Stop must never move backwards even if progressive lock is smaller."""
    engine = TrailingStopEngine(progressive_secure_percentage=0.5)
    
    # Current stop is already at 1.1040, proposed will be lower → should return None
    new_sl = engine.calculate_new_stop(
        direction="BUY", attempt_entry=1.1000, current_price=1.1050,
        current_hard_stop=1.1040, consumed_risk=0.0, take_profit=1.1200,
        lot_size=0.10, tick_value=1.0, tick_size=0.00001
    )
    assert new_sl is None, "Must not move stop backwards"

def test_position_sizing_engine():
    # Forex Example: EURUSD
    risk_amount = 10.0 # $10 risk
    entry = 1.1000
    sl = 1.0950 # 50 pips
    tick_value = 1.0 # Standard lot tick value varies, let's assume 1.0 for 1 pip
    tick_size = 0.0001
    
    # Distance = 0.0050 = 50 pips = 50 ticks
    # Risk = lot * 50 * 1.0 => 10 = lot * 50 => lot = 0.2
    
    lot_size = PositionSizingEngine.calculate_lot_size(
        risk_amount=risk_amount, entry_price=entry, stop_loss=sl,
        tick_value=tick_value, tick_size=tick_size
    )
    assert lot_size == 0.2
    
    # Max clamp test
    lot_size_max = PositionSizingEngine.calculate_lot_size(
        risk_amount=1000000.0, entry_price=entry, stop_loss=sl,
        tick_value=tick_value, tick_size=tick_size, lot_max=100.0
    )
    assert lot_size_max == 100.0

def test_daily_loss_limit(db_session):
    manager = PortfolioRiskManager(daily_dd_pct=0.07, max_account_risk_percent=10.0) # 7% limit
    
    # Add two losing trades today, total loss 60
    idea1 = TradeIdea(
        symbol="EURUSD", direction="BUY", source="test", original_entry=1.0,
        original_hard_stop=0.9, hard_stop=0.9, take_profit=1.1,
        entry_zone_low=0.9, entry_zone_high=1.1, max_idea_risk=30, max_retries=1,
        realized_pnl=-30.0, updated_at=datetime.now(timezone.utc)
    )
    idea2 = TradeIdea(
        symbol="GBPUSD", direction="BUY", source="test", original_entry=1.0,
        original_hard_stop=0.9, hard_stop=0.9, take_profit=1.1,
        entry_zone_low=0.9, entry_zone_high=1.1, max_idea_risk=30, max_retries=1,
        realized_pnl=-30.0, updated_at=datetime.now(timezone.utc)
    )
    db_session.add_all([idea1, idea2])
    db_session.commit()
    
    # Account equity is $1000. 7% is $70.
    # Current loss is $60. Should accept.
    can_accept = manager.can_accept_idea(
        db=db_session, symbol="XAUUSD", direction="BUY", hard_stop=1990,
        entry=2000, max_idea_risk=10, account_equity=1000.0
    )
    assert can_accept is True

    # Add another losing trade of $20. Total loss = $80.
    idea3 = TradeIdea(
        symbol="JPYUSD", direction="BUY", source="test", original_entry=1.0,
        original_hard_stop=0.9, hard_stop=0.9, take_profit=1.1,
        entry_zone_low=0.9, entry_zone_high=1.1, max_idea_risk=20, max_retries=1,
        realized_pnl=-20.0, updated_at=datetime.now(timezone.utc)
    )
    db_session.add(idea3)
    db_session.commit()

    # Total loss is $80. 7% of $1000 is $70. Should reject.
    can_accept_now = manager.can_accept_idea(
        db=db_session, symbol="XAUUSD", direction="BUY", hard_stop=1990,
        entry=2000, max_idea_risk=10, account_equity=1000.0
    )
    assert can_accept_now is False

def test_five_loss_circuit_breaker(db_session):
    manager = PortfolioRiskManager()
    
    # Add 5 consecutive losing trades
    for i in range(5):
        idea = TradeIdea(
            symbol=f"TEST{i}", direction="BUY", source="test", original_entry=1.0,
            original_hard_stop=0.9, hard_stop=0.9, take_profit=1.1,
            entry_zone_low=0.9, entry_zone_high=1.1, max_idea_risk=10, max_retries=1,
            realized_pnl=-10.0, state=TradeState.IDEA_INVALIDATED.value,
            updated_at=datetime.now(timezone.utc)
        )
        db_session.add(idea)
    
    db_session.commit()
    
    # System should be halted
    can_accept = manager.can_accept_idea(
        db=db_session, symbol="XAUUSD", direction="BUY", hard_stop=1990,
        entry=2000, max_idea_risk=10, account_equity=10000.0
    )
    assert can_accept is False

def test_portfolio_validation(db_session):
    manager = PortfolioRiskManager()
    
    # Invalid stop side for BUY
    assert not manager.can_accept_idea(db_session, "EURUSD", "BUY", hard_stop=1.2, entry=1.1, max_idea_risk=10)
    
    # Invalid stop side for SELL
    assert not manager.can_accept_idea(db_session, "EURUSD", "SELL", hard_stop=1.0, entry=1.1, max_idea_risk=10)
    
    # Negative risk
    assert not manager.can_accept_idea(db_session, "EURUSD", "BUY", hard_stop=1.0, entry=1.1, max_idea_risk=-10)

def test_final_hard_sl_breached_before_entry():
  from src.main import TradeManager

  buy = TradeIdea(
      symbol="XAUUSD", direction="BUY", source="test", original_entry=4352.0,
      original_hard_stop=4345.0, hard_stop=4345.0, take_profit=4365.0,
      entry_zone_low=4340.0, entry_zone_high=4360.0, max_idea_risk=10.0, max_retries=3,
  )
  sell = TradeIdea(
      symbol="XAUUSD", direction="SELL", source="test", original_entry=4352.0,
      original_hard_stop=4360.0, hard_stop=4360.0, take_profit=4340.0,
      entry_zone_low=4340.0, entry_zone_high=4360.0, max_idea_risk=10.0, max_retries=3,
  )

  # BUY: price at or below final hard SL invalidates, even during re-entry wait
  assert TradeManager._final_hard_sl_breached(buy, 4345.0)
  assert TradeManager._final_hard_sl_breached(buy, 4337.0)
  assert not TradeManager._final_hard_sl_breached(buy, 4346.0)

  # SELL: price at or above final hard SL invalidates
  assert TradeManager._final_hard_sl_breached(sell, 4360.0)
  assert TradeManager._final_hard_sl_breached(sell, 4365.0)
  assert not TradeManager._final_hard_sl_breached(sell, 4359.0)

def test_chop_exit_price_from_config():
    from src.core.config import TradingConfig

    cfg = TradingConfig(
        chop_exit_distance=1.0,
        symbol_chop_exit_distance={"EURUSD": 0.0001},
    )
    assert cfg.chop_exit_price("XAUUSD", 4352.0, "BUY") == 4351.0
    assert cfg.chop_exit_price("XAUUSD", 4352.0, "SELL") == 4353.0
    assert cfg.chop_exit_price("EURUSD", 1.0850, "BUY") == pytest.approx(1.0849)
    assert cfg.chop_stop_breached("XAUUSD", 4352.0, "BUY", 4351.0)
    assert cfg.chop_stop_breached("XAUUSD", 4352.0, "BUY", 4350.5)
    assert not cfg.chop_stop_breached("XAUUSD", 4352.0, "BUY", 4351.5)
    assert cfg.chop_stop_breached("XAUUSD", 4352.0, "SELL", 4353.0)
    assert not cfg.chop_stop_breached("XAUUSD", 4352.0, "SELL", 4352.5)
