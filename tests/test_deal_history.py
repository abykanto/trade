"""Tests for MT5 deal history parsing."""

from types import SimpleNamespace

import pytest

from src.market.deal_history import (
    PositionCloseDetails,
    close_details_from_deals,
    order_matches_attempt,
    pending_order_direction,
)


def test_pending_order_direction_mapping():
    assert pending_order_direction(2) == "BUY"
    assert pending_order_direction(4) == "BUY"
    assert pending_order_direction(3) == "SELL"
    assert pending_order_direction(5) == "SELL"
    assert pending_order_direction(99) is None


def test_order_matches_attempt_by_symbol_direction_price_volume():
    order = SimpleNamespace(
        symbol="XAUUSD",
        type=4,
        volume_current=0.05,
        price_open=4360.0,
    )
    assert order_matches_attempt(
        order,
        symbol="XAUUSD",
        direction="BUY",
        entry_price=4360.0,
        volume=0.05,
        tick_size=0.01,
    )
    assert not order_matches_attempt(
        order,
        symbol="XAUUSD",
        direction="SELL",
        entry_price=4360.0,
        volume=0.05,
        tick_size=0.01,
    )


def test_close_details_from_exit_deals():
    deals = [
        SimpleNamespace(entry=0, price=4360.0, profit=0.0, commission=0.0, swap=0.0, time=1),
        SimpleNamespace(entry=1, price=4355.0, profit=-12.5, commission=-0.5, swap=-0.2, time=2),
    ]
    details = close_details_from_deals(deals)
    assert details == PositionCloseDetails(
        close_price=4355.0,
        profit=-12.5,
        commission=-0.5,
        swap=-0.2,
    )
    assert details.net_profit == pytest.approx(-13.2)


def test_close_details_returns_none_without_deals():
    assert close_details_from_deals([]) is None
    assert close_details_from_deals(None) is None
