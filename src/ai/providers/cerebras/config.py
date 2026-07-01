"""Cerebras Cloud model names and per-model quota limits.

Limits from https://cloud.cerebras.ai (personal account tier).
Each model has its own quota bucket — rotating spreads load across them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelQuota:
    """Conservative per-model limits (slightly below dashboard caps)."""

    requests_per_minute: int = 4
    requests_per_hour: int = 140
    requests_per_day: int = 2300
    tokens_per_minute: int = 28_000
    tokens_per_hour: int = 950_000
    tokens_per_day: int = 950_000
    min_request_interval_sec: float = 13.0


# Dashboard models — add/remove here without touching core signal logic.
CEREBRAS_SIGNAL_MODELS: tuple[str, ...] = (
    "gemma-4-31b",
    "gpt-oss-120b",
    "zai-glm-4.7",
)

DEFAULT_MODEL_QUOTA = ModelQuota()

# Estimated completion budget for signal extraction calls.
DEFAULT_MAX_COMPLETION_TOKENS = 512
