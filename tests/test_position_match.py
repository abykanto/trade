"""Tests for MT5 position matching helpers."""

from types import SimpleNamespace

from src.market.position import find_idea_position


def _pos(ticket, magic=234000, type_=1, volume=0.04, price_open=1.15):
    return SimpleNamespace(
        ticket=ticket,
        magic=magic,
        type=type_,
        volume=volume,
        price_open=price_open,
    )


def test_find_idea_position_single_sell():
    positions = [_pos(9001, type_=1, volume=0.04, price_open=1.15)]
    assert find_idea_position(positions, "SELL", 234000, volume=0.04, entry_price=1.15) == 9001


def test_find_idea_position_ignores_wrong_magic():
    positions = [_pos(9001, magic=999)]
    assert find_idea_position(positions, "SELL", 234000) is None
