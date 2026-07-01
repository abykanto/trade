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
    
    # Phase 1: Price hasn't recovered idea deficit yet — NO trailing
    # idea_realized_pnl = -$3.50 → need $3.60 floating to lock recovery target
    new_sl = engine.calculate_new_stop(
        direction="BUY", attempt_entry=1000.50, current_price=1000.53,
        current_hard_stop=995.0, consumed_risk=3.50, take_profit=1012.0,
        lot_size=lot_size, tick_value=tick_value, tick_size=tick_size,
        idea_realized_pnl=-3.50,
    )
    assert new_sl is None, "Should NOT trail while still recovering idea deficit"
    
    # Phase 2: Price has recovered enough to lock idea net at target ($0.10)
    new_sl = engine.calculate_new_stop(
        direction="BUY", attempt_entry=1000.50, current_price=1004.50,
        current_hard_stop=995.0, consumed_risk=3.50, take_profit=1012.0,
        lot_size=lot_size, tick_value=tick_value, tick_size=tick_size,
        idea_realized_pnl=-3.50,
    )
    assert new_sl is not None, "Should trail after recovery"
    assert new_sl > 1000.50, "Stop must be above entry to lock in recovery"
    # Lock $3.60 → 0.036 price distance → ~1000.536
    assert new_sl == pytest.approx(1000.536, abs=0.001)

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


def test_resolve_volume_predefined_overrides_dynamic_cap():
    contract = __import__("src.market.contract", fromlist=["SymbolContract"]).SymbolContract.for_symbol("XAUUSD")
    volume, source = PositionSizingEngine.resolve_volume(
        contract, risk_amount=250.0, entry=4240.0, stop=4245.0,
        predefined_lot=0.03, max_lot_size=0.02,
    )
    assert source == "predefined"
    assert volume == 0.03


def test_resolve_volume_predefined_capped_by_remaining_risk():
    contract = __import__("src.market.contract", fromlist=["SymbolContract"]).SymbolContract.for_symbol("XAUUSD")
    volume, source = PositionSizingEngine.resolve_volume(
        contract, risk_amount=1.0, entry=4240.0, stop=4235.0,
        predefined_lot=0.10,
    )
    assert source == "predefined"
    assert volume < 0.10


def test_resolve_volume_dynamic_capped_at_max_lot():
    contract = __import__("src.market.contract", fromlist=["SymbolContract"]).SymbolContract.for_symbol("XAUUSD")
    volume, source = PositionSizingEngine.resolve_volume(
        contract, risk_amount=25.0, entry=4240.0, stop=4245.0,
        predefined_lot=None, max_lot_size=0.02,
    )
    assert source == "dynamic"
    assert volume == 0.02


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
    
    # Add 5 consecutive losing trades (most recent first in query order)
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


def test_five_loss_circuit_breaker_resets_after_win(db_session):
    manager = PortfolioRiskManager()

    win = TradeIdea(
        symbol="WIN", direction="BUY", source="test", original_entry=1.0,
        original_hard_stop=0.9, hard_stop=0.9, take_profit=1.1,
        entry_zone_low=0.9, entry_zone_high=1.1, max_idea_risk=10, max_retries=1,
        realized_pnl=5.0, state=TradeState.TP_REACHED.value,
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(win)
    for i in range(4):
        idea = TradeIdea(
            symbol=f"LOSS{i}", direction="BUY", source="test", original_entry=1.0,
            original_hard_stop=0.9, hard_stop=0.9, take_profit=1.1,
            entry_zone_low=0.9, entry_zone_high=1.1, max_idea_risk=10, max_retries=1,
            realized_pnl=-10.0, state=TradeState.IDEA_INVALIDATED.value,
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(idea)
    db_session.commit()

    can_accept = manager.can_accept_idea(
        db=db_session, symbol="XAUUSD", direction="BUY", hard_stop=1990,
        entry=2000, max_idea_risk=10, account_equity=10000.0,
    )
    assert can_accept is True


def test_reentry_not_blocked_when_at_max_active_including_self(db_session):
    """Re-arming an existing idea must not count against max_active_ideas."""
    manager = PortfolioRiskManager(max_active_ideas=2)

    reentry = TradeIdea(
        symbol="XAUUSD", direction="BUY", source="test", original_entry=1.0,
        original_hard_stop=0.9, hard_stop=0.9, take_profit=1.1,
        entry_zone_low=0.9, entry_zone_high=1.1, max_idea_risk=10, max_retries=5,
        state=TradeState.WAITING_FOR_REENTRY.value,
    )
    other = TradeIdea(
        symbol="EURUSD", direction="BUY", source="test2", original_entry=1.0,
        original_hard_stop=0.9, hard_stop=0.9, take_profit=1.1,
        entry_zone_low=0.9, entry_zone_high=1.1, max_idea_risk=10, max_retries=1,
        state=TradeState.WAITING_FOR_SETUP.value,
    )
    db_session.add_all([reentry, other])
    db_session.commit()

    can_accept = manager.can_accept_idea(
        db=db_session, symbol="XAUUSD", direction="BUY", hard_stop=0.9,
        entry=1.0, max_idea_risk=5.0, account_equity=10000.0,
        exclude_idea_id=reentry.id,
    )
    assert can_accept is True


def test_rejects_when_remaining_risk_zero(db_session):
    manager = PortfolioRiskManager()
    assert not manager.can_accept_idea(
        db_session, "XAUUSD", "BUY", hard_stop=0.9, entry=1.0,
        max_idea_risk=0.0, account_equity=10000.0,
    )


def test_entry_zone_mid_price_check():
    from src.market.pending_entry import ready_for_initial_entry_placement

    # Inside zone → always ready
    assert ready_for_initial_entry_placement(
        "BUY", 4240.0, 4239.9, 4240.1, 4239.0, 4241.0, tick_size=0.01,
    )
    # Below entry but resting buy-limit possible → ready (not blocked by zone)
    assert ready_for_initial_entry_placement(
        "BUY", 4240.0, 4235.0, 4235.2, 4239.0, 4241.0, tick_size=0.01,
    )
    # Far above entry, buy-limit can still rest → ready
    assert ready_for_initial_entry_placement(
        "BUY", 4240.0, 4250.0, 4250.2, 4239.0, 4241.0, tick_size=0.01,
    )

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

def test_resolve_position_ticket_by_identifier():
    from types import SimpleNamespace
    from src.main import TradeManager

    positions = [
        SimpleNamespace(identifier=100, ticket=200, volume=0.05),
    ]
    assert TradeManager._resolve_position_ticket(positions, 100) == 200
    assert TradeManager._resolve_position_ticket(positions, 999) is None

def test_resolve_position_ticket_rejects_order_ticket():
    from types import SimpleNamespace
    from src.market.position import resolve_position_ticket

    positions = [
        SimpleNamespace(identifier=100, ticket=100, volume=0.02, type=0, price_open=4290.0),
        SimpleNamespace(identifier=100, ticket=200, volume=0.02, type=0, price_open=4290.0),
    ]
    assert resolve_position_ticket(positions, 100) == 200
    assert resolve_position_ticket([positions[0]], 100) is None

    from types import SimpleNamespace
    from src.main import TradeManager

    positions = [
        SimpleNamespace(identifier=0, ticket=3135563212, volume=0.05, type=1, price_open=4290.0),
    ]
    assert TradeManager._resolve_position_ticket(
        positions, order_ticket=999,
        direction="SELL", volume=0.05, entry_price=4290.0,
    ) == 3135563212

def test_position_stop_matches():
    from types import SimpleNamespace
    from src.execution.bridge import MT5Bridge

    pos = SimpleNamespace(sl=4291.0)
    assert MT5Bridge.position_stop_matches(pos, 4291.0, tick_size=0.01)
    assert not MT5Bridge.position_stop_matches(pos, 4292.0, tick_size=0.01)
    assert not MT5Bridge.position_stop_matches(SimpleNamespace(sl=0.0), 4291.0)

def test_pre_entry_sl_price_uses_ask_for_sell():
    from src.main import TradeManager
    from src.core.models import TradeIdea

    sell = TradeIdea(
        symbol="XAUUSD", direction="SELL", source="t", original_entry=4290.0,
        original_hard_stop=4300.0, hard_stop=4300.0, take_profit=4270.0,
        entry_zone_low=4280.0, entry_zone_high=4300.0, max_idea_risk=5.0, max_retries=3,
    )
    # Bid below final SL but ask above → SELL idea must invalidate
    sl_price = TradeManager._pre_entry_sl_price(sell, bid=4299.5, ask=4300.2)
    assert TradeManager._final_hard_sl_breached(sell, sl_price)
    # Bid-only check would miss this breach
    assert not TradeManager._final_hard_sl_breached(sell, 4299.5)

def test_chop_exit_price_from_config():
    from src.core.config import TradingConfig
    from src.market import ChopRules

    cfg = TradingConfig(chop=ChopRules(
        default_distance=1.0,
        per_symbol={"EURUSD": 0.0001},
    ))
    assert cfg.chop_exit_price("XAUUSD", 4352.0, "BUY") == 4351.0
    assert cfg.chop_exit_price("XAUUSD", 4352.0, "SELL") == 4353.0
    assert cfg.chop_exit_price("EURUSD", 1.0850, "BUY") == pytest.approx(1.0849)
    assert cfg.chop_stop_breached("XAUUSD", 4352.0, "BUY", 4351.0)
    assert cfg.chop_stop_breached("XAUUSD", 4352.0, "BUY", 4350.5)
    assert not cfg.chop_stop_breached("XAUUSD", 4352.0, "BUY", 4351.5)
    assert cfg.chop_stop_breached("XAUUSD", 4352.0, "SELL", 4353.0)
    assert not cfg.chop_stop_breached("XAUUSD", 4352.0, "SELL", 4352.5)

def test_dollar_pnl_xauusd():
    # 0.05 lot, $1 move ≈ $5 (tick_size=0.01, tick_value=1.0)
    pnl = PositionSizingEngine.calculate_dollar_pnl(
        "BUY", 4360.0, 4359.0, 0.05, tick_value=1.0, tick_size=0.01
    )
    assert pnl == pytest.approx(-5.0)

def test_recovery_trailing_xauusd_scenario():
    """Idea #4 attempt 3: after +$3 and -$5, lock ~$2.10 for +$0.10 idea net."""
    engine = TrailingStopEngine(target_net_profit=0.10)
    # idea realized = -$2.00 before attempt 3
    new_sl = engine.calculate_new_stop(
        direction="BUY", attempt_entry=4360.0, current_price=4362.0,
        current_hard_stop=4359.0, consumed_risk=5.0, take_profit=4370.0,
        lot_size=0.04, tick_value=1.0, tick_size=0.01,
        idea_realized_pnl=-2.0,
    )
    assert new_sl is not None
    # needed = 0.10 - (-2.0) = 2.10 → 2.10/4 = 0.525 → SL 4360.525
    assert new_sl == pytest.approx(4360.525, abs=0.01)

def test_classify_stop_exit():
    from src.main import TradeManager
    from src.market import SymbolContract

    xau = SymbolContract.for_symbol("XAUUSD")
    assert TradeManager._classify_stop_exit("BUY", 4359.0, 4359.0, 4360.6, xau) == "CHOP_EXIT"
    assert TradeManager._classify_stop_exit("BUY", 4360.6, 4359.0, 4360.6, xau) == "TRAILING_STOP"

def test_pending_order_type_rules():
    """BUY above market → stop; BUY below market → limit."""
    from unittest.mock import MagicMock
    from src.execution.bridge import MT5Bridge
    from src.market.pending_entry import PendingOrderKind

    bridge = MT5Bridge()
    mt5 = MagicMock()
    mt5.ORDER_TYPE_BUY_LIMIT = 2
    mt5.ORDER_TYPE_BUY_STOP = 4
    mt5.ORDER_TYPE_SELL_LIMIT = 3
    mt5.ORDER_TYPE_SELL_STOP = 5
    bridge._get_mt5 = lambda: mt5

    assert bridge.expected_pending_order_type("BUY", 4300.0, 4302.0, 4302.5) == 2
    assert bridge.expected_pending_order_type("BUY", 4300.0, 4299.0, 4299.5) == 4
    assert bridge.expected_pending_order_type("SELL", 4300.0, 4302.0, 4302.5) == 5
    assert bridge.expected_pending_order_type("SELL", 4300.0, 4298.0, 4298.5) == 3

    plan = bridge.plan_pending_entry("BUY", 4290.0, 4277.0, 4277.2, tick_size=0.01)
    assert plan.kind == PendingOrderKind.BUY_STOP
    assert not plan.would_fill_immediately

def test_api_rejects_sell_with_buy_levels():
    from fastapi import HTTPException
    from src.api.server import _validate_signal_levels

    with pytest.raises(HTTPException) as exc:
        _validate_signal_levels("SELL", 4250.0, 4240.0, 4270.0)
    assert "BUY signal" in exc.value.detail

def test_api_accepts_sell_levels():
    from src.api.server import _validate_signal_levels
    _validate_signal_levels("SELL", 4250.0, 4260.0, 4230.0)


def test_next_attempt_number_increments_per_idea():
    from unittest.mock import MagicMock
    from src.main import TradeManager

    session = MagicMock()
    session.query.return_value.filter_by.return_value.scalar.return_value = 3
    assert TradeManager._next_attempt_number(session, 1) == 4
    session.query.return_value.filter_by.return_value.scalar.return_value = None
    assert TradeManager._next_attempt_number(session, 1) == 1
