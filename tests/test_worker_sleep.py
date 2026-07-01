"""Tests for adaptive symbol-worker sleep intervals."""

from src.core.models import TradeIdea, TradeState
from src.main import TradeManager


def _idea(state: str, symbol: str = "XAUUSD") -> TradeIdea:
    idea = TradeIdea(
        symbol=symbol,
        direction="BUY",
        original_entry=4360.0,
        original_hard_stop=4350.0,
        take_profit=4380.0,
        max_idea_risk=10.0,
        state=state,
    )
    return idea


def test_worker_sleep_fast_for_pre_entry():
    mgr = TradeManager(db_url="sqlite:///:memory:")
    ideas = [_idea(TradeState.WAITING_FOR_SETUP.value)]
    assert mgr._worker_sleep_sec("XAUUSD", ideas) == 0.01


def test_worker_sleep_idle_when_no_active_ideas():
    mgr = TradeManager(db_url="sqlite:///:memory:")
    assert mgr._worker_sleep_sec("XAUUSD", []) == 0.1


def test_worker_sleep_zero_on_ea_wake():
    mgr = TradeManager(db_url="sqlite:///:memory:")
    mgr._ea_symbol_wake.add("XAUUSD")
    assert mgr._worker_sleep_sec("XAUUSD", []) == 0.0
    assert "XAUUSD" not in mgr._ea_symbol_wake
