"""Provider-agnostic interface for AI-backed signal extraction."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SignalAIProvider(Protocol):
    """Extract structured signal JSON from a Telegram-style message."""

    provider_name: str

    def extract_signal_json(self, message: str) -> dict[str, Any]:
        """Return parsed JSON dict from the model (valid_signal + fields or reason)."""
