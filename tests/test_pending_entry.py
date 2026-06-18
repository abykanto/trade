"""Tests for pending entry order planning."""

import pytest

from src.market.pending_entry import (
    PendingOrderKind,
    entry_inside_spread,
    entry_market_side,
    fill_price_violates_entry,
    pending_would_fill_immediately,
    plan_pending_entry,
    select_pending_order_kind,
)


def test_buy_above_market_uses_buy_stop():
    kind = select_pending_order_kind("BUY", 4290.0, bid=4277.0, ask=4277.2)
    assert kind == PendingOrderKind.BUY_STOP
    assert not pending_would_fill_immediately(kind, 4290.0, 4277.0, 4277.2)


def test_buy_limit_refused_when_ask_below_entry():
    kind = PendingOrderKind.BUY_LIMIT
    assert pending_would_fill_immediately(kind, 4290.0, 4277.0, 4277.2)


def test_buy_limit_rests_when_ask_above_entry():
    kind = PendingOrderKind.BUY_LIMIT
    assert not pending_would_fill_immediately(kind, 4290.0, 4291.0, 4291.5)


def test_buy_stop_refused_when_ask_at_entry():
    kind = PendingOrderKind.BUY_STOP
    assert pending_would_fill_immediately(kind, 4290.0, 4289.0, 4290.0)


def test_sell_below_market_uses_sell_stop():
    kind = select_pending_order_kind("SELL", 4290.0, bid=4300.0, ask=4300.2)
    assert kind == PendingOrderKind.SELL_STOP


def test_sell_above_market_uses_sell_limit():
    kind = select_pending_order_kind("SELL", 1.15, bid=1.14, ask=1.1402)
    assert kind == PendingOrderKind.SELL_LIMIT
    assert not pending_would_fill_immediately(kind, 1.15, 1.14, 1.1402)


def test_entry_market_side_for_reentry():
    assert entry_market_side("SELL", 1.15, bid=1.16, ask=1.1602) == "market_above_entry"
    assert entry_market_side("SELL", 1.15, bid=1.14, ask=1.1402) == "market_below_entry"
    assert entry_market_side("BUY", 4290.0, bid=4277.0, ask=4277.2) == "market_below_entry"
    assert entry_market_side("BUY", 4290.0, bid=4291.0, ask=4291.5) == "market_above_entry"


def test_plan_cannot_place_without_mt5_type():
    plan = plan_pending_entry("BUY", 4290.0, 4277.0, 4277.2, tick_size=0.01)
    assert plan.kind == PendingOrderKind.BUY_STOP
    assert not plan.would_fill_immediately
    assert plan.can_place is False  # mt5 type not wired


def test_fill_price_violation_detects_market_dump():
    assert fill_price_violates_entry("BUY", 4290.0, 4277.0, tick_size=0.01)
    assert not fill_price_violates_entry("BUY", 4290.0, 4290.05, tick_size=0.01)


def test_entry_inside_spread_defers():
    from src.market.pending_entry import entry_inside_spread, select_pending_order_kind

    assert entry_inside_spread(1.1482, 1.1481, 1.1483, tick_size=0.00001)
    kind = select_pending_order_kind("BUY", 1.1482, 1.1481, 1.1483, tick_size=0.00001)
    assert pending_would_fill_immediately(kind, 1.1482, 1.1481, 1.1483, tick_size=0.00001)
