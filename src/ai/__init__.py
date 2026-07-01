"""AI integrations for text-message signal processing.

Core signal logic is provider-agnostic under ``src.ai.text_signals``.
Provider-specific code lives under ``src.ai.providers`` and can be removed
independently (e.g. delete ``providers/cerebras/`` when switching vendors).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from src.ai.protocol import SignalAIProvider

if TYPE_CHECKING:
    pass


def create_signal_ai_provider(
    provider: str | None = None,
) -> SignalAIProvider | None:
    """
    Factory for the configured AI provider.

    Set ``SIGNAL_AI_PROVIDER=cerebras`` (default) or ``none`` to disable AI.
    Requires ``CEREBRAS_API_KEY`` or ``CEREBRAS_API_KEYS`` when using Cerebras.
    """
    name = (provider or os.environ.get("SIGNAL_AI_PROVIDER", "cerebras")).strip().lower()
    if name in {"", "none", "off", "disabled"}:
        return None
    if name == "cerebras":
        from src.ai.providers.cerebras import CerebrasSignalExtractor

        extractor = CerebrasSignalExtractor()
        if not extractor.is_available:
            return None
        return extractor
    raise ValueError(f"Unknown SIGNAL_AI_PROVIDER: {name!r}")


__all__ = [
    "SignalAIProvider",
    "create_signal_ai_provider",
]
