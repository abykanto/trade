"""Tests for pending order outcome resolution."""

from types import SimpleNamespace

from src.market.order_outcome import (
    ORDER_STATE_CANCELED,
    ORDER_STATE_FILLED,
    resolve_pending_order_outcome,
)


def _order(ticket, magic=234000):
    return SimpleNamespace(ticket=ticket, magic=magic)


def _pos(ticket, identifier, magic=234000, volume=0.02, price_open=1.1482, type_=0):
    return SimpleNamespace(
        ticket=ticket,
        identifier=identifier,
        magic=magic,
        volume=volume,
        price_open=price_open,
        type=type_,
        sl=0.0,
    )


def test_pending_order_still_resting():
    outcome = resolve_pending_order_outcome(
        order_ticket=100,
        symbol="EURUSD",
        direction="BUY",
        volume=0.02,
        entry_price=1.1482,
        tick_size=0.00001,
        magic=234000,
        orders=[_order(100)],
        positions=[],
        history_order=None,
        order_deals=None,
    )
    assert outcome.status == "pending"


def test_filled_order_open_position():
    outcome = resolve_pending_order_outcome(
        order_ticket=100,
        symbol="EURUSD",
        direction="BUY",
        volume=0.02,
        entry_price=1.1482,
        tick_size=0.00001,
        magic=234000,
        orders=[],
        positions=[_pos(ticket=200, identifier=100, price_open=1.1482)],
        history_order=SimpleNamespace(state=ORDER_STATE_FILLED),
        order_deals=None,
    )
    assert outcome.status == "open"
    assert outcome.position_ticket == 200
    assert outcome.fill_price == 1.1482


def test_cancelled_order_detected():
    outcome = resolve_pending_order_outcome(
        order_ticket=100,
        symbol="EURUSD",
        direction="BUY",
        volume=0.02,
        entry_price=1.1482,
        tick_size=0.00001,
        magic=234000,
        orders=[],
        positions=[],
        history_order=SimpleNamespace(
            state=ORDER_STATE_CANCELED,
            comment="TradeIdeaBot_Pending",
            magic=234000,
        ),
        order_deals=None,
    )
    assert outcome.status == "cancelled"
    assert outcome.order_comment == "TradeIdeaBot_Pending"
    assert outcome.order_magic == 234000


def test_filled_and_closed_from_deals():
    deals = [
        SimpleNamespace(
            entry=0, price=1.1492, position_id=300, time=1,
        ),
        SimpleNamespace(
            entry=1, price=1.1491, position_id=300, time=2,
        ),
    ]
    outcome = resolve_pending_order_outcome(
        order_ticket=100,
        symbol="EURUSD",
        direction="BUY",
        volume=0.04,
        entry_price=1.1492,
        tick_size=0.00001,
        magic=234000,
        orders=[],
        positions=[],
        history_order=SimpleNamespace(state=ORDER_STATE_FILLED),
        order_deals=deals,
    )
    assert outcome.status == "closed"
    assert outcome.fill_price == 1.1492
    assert outcome.close_price == 1.1491
