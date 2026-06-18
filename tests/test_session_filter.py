"""Tests for prime session filter toggle."""

from src.risk.quant_filters import SessionManager, _env_flag


def test_prime_session_filter_disabled_allows_any_hour():
    sm = SessionManager(prime_session_filter=False)
    assert sm.is_in_prime_session("EURUSD") is True
    assert sm.is_in_prime_session("XAUUSD") is True


def test_env_flag_parsing():
    assert _env_flag("X", True) is True
    assert _env_flag("X", False) is False
