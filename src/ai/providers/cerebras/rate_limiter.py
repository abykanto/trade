"""Per-model rate tracking and round-robin model selection for Cerebras."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from src.ai.providers.cerebras.config import (
    CEREBRAS_SIGNAL_MODELS,
    DEFAULT_MODEL_QUOTA,
    ModelQuota,
)


@dataclass
class _ModelUsage:
    requests: deque[float] = field(default_factory=deque)
    token_events: deque[tuple[float, int]] = field(default_factory=deque)
    last_request_at: float = 0.0
    rate_limited_until: float = 0.0


class CerebrasModelRateLimiter:
    """Track usage per model and pick the least-loaded model with capacity."""

    def __init__(
        self,
        models: tuple[str, ...] = CEREBRAS_SIGNAL_MODELS,
        quota: ModelQuota = DEFAULT_MODEL_QUOTA,
    ):
        self.models = models
        self.quota = quota
        self._lock = threading.Lock()
        self._usage: dict[str, _ModelUsage] = defaultdict(_ModelUsage)

    @staticmethod
    def estimate_tokens(message: str, max_completion_tokens: int) -> int:
        # Rough prompt estimate: ~4 chars per token + completion budget.
        prompt_tokens = max(1, len(message) // 4)
        return prompt_tokens + max_completion_tokens

    def _prune(self, usage: _ModelUsage, now: float) -> None:
        minute_ago = now - 60.0
        hour_ago = now - 3600.0
        day_ago = now - 86400.0

        while usage.requests and usage.requests[0] < day_ago:
            usage.requests.popleft()
        while usage.token_events and usage.token_events[0][0] < day_ago:
            usage.token_events.popleft()

    def _counts(self, usage: _ModelUsage, now: float) -> tuple[int, int, int, int, int, int]:
        minute_ago = now - 60.0
        hour_ago = now - 3600.0

        req_min = sum(1 for ts in usage.requests if ts >= minute_ago)
        req_hour = sum(1 for ts in usage.requests if ts >= hour_ago)
        req_day = len(usage.requests)

        tok_min = sum(tokens for ts, tokens in usage.token_events if ts >= minute_ago)
        tok_hour = sum(tokens for ts, tokens in usage.token_events if ts >= hour_ago)
        tok_day = sum(tokens for _, tokens in usage.token_events)

        return req_min, req_hour, req_day, tok_min, tok_hour, tok_day

    def _has_capacity(self, usage: _ModelUsage, estimated_tokens: int, now: float) -> bool:
        if now < usage.rate_limited_until:
            return False
        if usage.last_request_at and (
            now - usage.last_request_at
        ) < self.quota.min_request_interval_sec:
            return False

        req_min, req_hour, req_day, tok_min, tok_hour, tok_day = self._counts(usage, now)
        q = self.quota
        return (
            req_min < q.requests_per_minute
            and req_hour < q.requests_per_hour
            and req_day < q.requests_per_day
            and tok_min + estimated_tokens <= q.tokens_per_minute
            and tok_hour + estimated_tokens <= q.tokens_per_hour
            and tok_day + estimated_tokens <= q.tokens_per_day
        )

    def _seconds_until_capacity(
        self, usage: _ModelUsage, estimated_tokens: int, now: float
    ) -> float:
        if now < usage.rate_limited_until:
            return usage.rate_limited_until - now

        waits: list[float] = []
        if usage.last_request_at:
            gap = self.quota.min_request_interval_sec - (now - usage.last_request_at)
            if gap > 0:
                waits.append(gap)

        minute_ago = now - 60.0
        hour_ago = now - 3600.0
        day_ago = now - 86400.0

        req_min, req_hour, req_day, tok_min, tok_hour, tok_day = self._counts(usage, now)
        q = self.quota

        if req_min >= q.requests_per_minute and usage.requests:
            for ts in usage.requests:
                if ts >= minute_ago:
                    waits.append(60.0 - (now - ts))
                    break
        if req_hour >= q.requests_per_hour and usage.requests:
            for ts in usage.requests:
                if ts >= hour_ago:
                    waits.append(3600.0 - (now - ts))
                    break
        if req_day >= q.requests_per_day and usage.requests:
            waits.append(86400.0 - (now - usage.requests[0]))

        if tok_min + estimated_tokens > q.tokens_per_minute:
            running = tok_min
            for ts, tokens in reversed(usage.token_events):
                if ts < minute_ago:
                    break
                running -= tokens
                if running + estimated_tokens <= q.tokens_per_minute:
                    waits.append(60.0 - (now - ts))
                    break

        if tok_hour + estimated_tokens > q.tokens_per_hour:
            running = tok_hour
            for ts, tokens in reversed(usage.token_events):
                if ts < hour_ago:
                    break
                running -= tokens
                if running + estimated_tokens <= q.tokens_per_hour:
                    waits.append(3600.0 - (now - ts))
                    break

        if tok_day + estimated_tokens > q.tokens_per_day:
            running = tok_day
            for ts, tokens in reversed(usage.token_events):
                if ts < day_ago:
                    break
                running -= tokens
                if running + estimated_tokens <= q.tokens_per_day:
                    waits.append(86400.0 - (now - ts))
                    break

        return max(waits) if waits else 0.0

    def _pick_model_locked(
        self,
        estimated_tokens: int,
        now: float,
        exclude: set[str] | None = None,
    ) -> str | None:
        excluded = exclude or set()
        candidates: list[tuple[int, str]] = []
        for model in self.models:
            if model in excluded:
                continue
            usage = self._usage[model]
            self._prune(usage, now)
            if not self._has_capacity(usage, estimated_tokens, now):
                continue
            req_min, _, _, _, _, _ = self._counts(usage, now)
            candidates.append((req_min, model))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][1]

    def acquire_model(
        self,
        estimated_tokens: int,
        *,
        max_wait_sec: float = 120.0,
        exclude: set[str] | None = None,
    ) -> str:
        """Block until a model has quota, then reserve it."""
        excluded = set(exclude or ())
        deadline = time.monotonic() + max_wait_sec
        while time.monotonic() < deadline:
            with self._lock:
                now = time.monotonic()
                model = self._pick_model_locked(estimated_tokens, now, excluded)
                if model is not None:
                    usage = self._usage[model]
                    usage.requests.append(now)
                    usage.token_events.append((now, estimated_tokens))
                    usage.last_request_at = now
                    return model

                wait = min(
                    (
                        self._seconds_until_capacity(self._usage[m], estimated_tokens, now)
                        for m in self.models
                        if m not in excluded
                    ),
                    default=1.0,
                )
            time.sleep(min(max(wait, 0.05), 2.0, deadline - time.monotonic()))

        raise RuntimeError(
            "All Cerebras models are rate-limited. "
            f"Try again later or reduce message volume."
        )

    def mark_rate_limited(self, model: str, cooldown_sec: float = 60.0) -> None:
        with self._lock:
            usage = self._usage[model]
            usage.rate_limited_until = max(
                usage.rate_limited_until,
                time.monotonic() + cooldown_sec,
            )
