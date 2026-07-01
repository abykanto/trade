"""Cerebras SDK client pool — one client per API key, rotate on rate limits."""

from __future__ import annotations

import threading
import time
from typing import Any

from src.ai.providers.cerebras.keys import load_cerebras_api_keys

_pool = None


class CerebrasClientPool:
    """Round-robin across API keys; each key has independent Cerebras quota."""

    def __init__(self, api_keys: tuple[str, ...]):
        if not api_keys:
            raise ValueError("At least one Cerebras API key is required")
        from cerebras.cloud.sdk import Cerebras

        self.api_keys = api_keys
        self._clients = [Cerebras(api_key=key) for key in api_keys]
        self._rate_limited_until = [0.0] * len(api_keys)
        self._lock = threading.Lock()
        self._rr = 0

    @classmethod
    def from_env(cls) -> CerebrasClientPool | None:
        keys = load_cerebras_api_keys()
        if not keys:
            return None
        return cls(keys)

    def __len__(self) -> int:
        return len(self._clients)

    def client(self, key_index: int) -> Any:
        return self._clients[key_index]

    def acquire_key_index(
        self,
        exclude: set[int] | None = None,
        *,
        max_wait_sec: float = 120.0,
    ) -> int:
        """Pick the next key with capacity (round-robin among available keys)."""
        excluded = exclude or set()
        deadline = time.monotonic() + max_wait_sec

        while time.monotonic() < deadline:
            with self._lock:
                now = time.monotonic()
                n = len(self._clients)
                for offset in range(n):
                    idx = (self._rr + offset) % n
                    if idx in excluded:
                        continue
                    if now >= self._rate_limited_until[idx]:
                        self._rr = (idx + 1) % n
                        return idx
                wait = min(
                    (
                        self._rate_limited_until[i] - now
                        for i in range(n)
                        if i not in excluded
                    ),
                    default=1.0,
                )
            time.sleep(min(max(wait, 0.05), 2.0, deadline - time.monotonic()))

        raise RuntimeError(
            "All Cerebras API keys are rate-limited. Try again later."
        )

    def mark_rate_limited(self, key_index: int, cooldown_sec: float = 60.0) -> None:
        with self._lock:
            self._rate_limited_until[key_index] = max(
                self._rate_limited_until[key_index],
                time.monotonic() + cooldown_sec,
            )


def get_cerebras_client_pool() -> CerebrasClientPool | None:
    """Return a shared client pool (one client per configured API key)."""
    global _pool
    if _pool is None:
        _pool = CerebrasClientPool.from_env()
    return _pool


def get_cerebras_client() -> Any | None:
    """Return the first pool client (backward compatibility)."""
    pool = get_cerebras_client_pool()
    if pool is None:
        return None
    return pool.client(0)


def reset_cerebras_client() -> None:
    """Clear cached pool (tests)."""
    global _pool
    _pool = None
