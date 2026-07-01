"""Execution backend selection."""

from src.execution.factory import create_execution_bridge
from src.execution.bridge import MT5Bridge
from src.execution.ea_bridge import EABridge


def test_factory_defaults_to_mt5linux(monkeypatch):
    monkeypatch.delenv("EXECUTION_BACKEND", raising=False)
    bridge = create_execution_bridge()
    assert isinstance(bridge, MT5Bridge)


def test_factory_ea_backend(monkeypatch):
    monkeypatch.setenv("EXECUTION_BACKEND", "ea")
    bridge = create_execution_bridge()
    assert isinstance(bridge, EABridge)
