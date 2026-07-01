"""Factory for execution backends — keeps legacy mt5linux and new EA paths side by side."""

from __future__ import annotations

import os

from src.execution.bridge import MT5Bridge
from src.execution.ea_bridge import EABridge


def create_execution_bridge():
    """Return the configured execution backend.

    Environment:
      EXECUTION_BACKEND=mt5linux  (default) — Python mt5linux RPyC bridge (reference impl)
      EXECUTION_BACKEND=ea        — MQL5 EA over TCP (see mql5/README.md)
    """
    backend = os.environ.get("EXECUTION_BACKEND", "mt5linux").strip().lower()
    if backend in ("ea", "mql5", "expert"):
        return EABridge()
    return MT5Bridge()
