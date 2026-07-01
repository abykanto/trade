"""Load one or more Cerebras API keys from the environment."""

from __future__ import annotations

import os
import re


def load_cerebras_api_keys() -> tuple[str, ...]:
    """Return deduplicated API keys (order preserved).

    Supports:
    - ``CEREBRAS_API_KEYS`` — comma/semicolon/whitespace-separated list
    - ``CEREBRAS_API_KEY`` — single key (merged if not already listed)
    """
    seen: dict[str, None] = {}

    def add(key: str) -> None:
        k = key.strip()
        if k:
            seen.setdefault(k, None)

    bulk = os.environ.get("CEREBRAS_API_KEYS", "").strip()
    if bulk:
        for part in re.split(r"[,;\s]+", bulk):
            add(part)

    add(os.environ.get("CEREBRAS_API_KEY", ""))

    return tuple(seen.keys())
