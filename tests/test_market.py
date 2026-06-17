"""Tests for the shared market math layer (symbol-agnostic conversions)."""

import pytest

from src.market import (
    ChopRules,
    Direction,
    MarketTick,
    SymbolContract,
    TradeLevels,
    resolve_position_ticket,
)


def test_xauusd_dollar_pnl_one_point():
    """XAUUSD: 0.05 lots, $1 adverse move → $5 (same formula as all symbols)."""
    contract = SymbolContract.for_symbol("XAUUSD")
    pnl = contract.dollar_pnl("BUY", 4300.0, 4299.0, 0.05)
    assert pnl == pytest.approx(-5.0)
    pnl_sell = contract.dollar_pnl("SELL", 4290.0, 4291.0, 0.05)
    assert pnl_sell == pytest.approx(-5.0)


def test_eurusd_dollar_pnl_pip():
    contract = SymbolContract.for_symbol("EURUSD")
    # 10 pips (0.00010) on 0.10 lots
    pnl = contract.dollar_pnl("BUY", 1.1000, 1.0990, 0.10)
    assert pnl == pytest.approx(-10.0)


def test_dollars_to_price_distance_roundtrip():
    contract = SymbolContract.for_symbol("XAUUSD")
    dist = contract.price_distance_for_dollars(5.0, 0.05)
    assert dist == pytest.approx(1.0)
    pnl = contract.dollar_pnl("BUY", 4300.0, 4300.0 - dist, 0.05)
    assert pnl == pytest.approx(-5.0)


def test_market_tick_stop_and_tp_sides():
    tick = MarketTick(bid=4299.5, ask=4300.2)
    assert tick.stop_trigger_price("SELL") == 4300.2
    assert tick.stop_trigger_price("BUY") == 4299.5
    assert tick.tp_trigger_price("SELL") == 4299.5
    assert tick.close_price("BUY") == 4299.5
    assert tick.close_price("SELL") == 4300.2


def test_chop_rules_per_symbol():
    rules = ChopRules.from_config_data(1.0, {"EURUSD": 0.0001, "XAUUSD": 1.0})
    assert rules.exit_price("XAUUSD", 4352.0, "BUY") == 4351.0
    assert rules.exit_price("EURUSD", 1.0850, "BUY") == pytest.approx(1.0849)


def test_trade_levels_hard_sl_and_tp():
    assert TradeLevels.final_hard_sl_breached("SELL", 4300.0, 4300.2)
    assert not TradeLevels.final_hard_sl_breached("SELL", 4300.0, 4299.5)
    assert TradeLevels.take_profit_breached("BUY", 4315.0, 4315.0)


def test_direction_stop_side_validation():
    assert Direction.BUY.stop_side_valid(4300.0, 4299.0)
    assert not Direction.SELL.stop_side_valid(4290.0, 4289.0)


def test_resolve_position_ticket_fallback():
    from types import SimpleNamespace

    positions = [
        SimpleNamespace(identifier=0, ticket=42, volume=0.05, type=1, price_open=4290.0),
    ]
    assert resolve_position_ticket(
        positions, 999, direction="SELL", volume=0.05, entry_price=4290.0
    ) == 42
