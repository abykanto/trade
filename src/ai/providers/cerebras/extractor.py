"""Cerebras-backed signal extraction with multi-key and multi-model rotation.

Delete this package when switching AI providers — core logic lives in
``src.ai.text_signals`` and only depends on ``SignalAIProvider``.
"""

from __future__ import annotations

import logging
from typing import Any

from src.ai.protocol import SignalAIProvider
from src.ai.providers.cerebras.client import CerebrasClientPool, get_cerebras_client_pool
from src.ai.providers.cerebras.config import (
    CEREBRAS_SIGNAL_MODELS,
    DEFAULT_MAX_COMPLETION_TOKENS,
)
from src.ai.providers.cerebras.rate_limiter import CerebrasModelRateLimiter
from src.ai.text_signals.processor import parse_ai_json
from src.ai.text_signals.prompts import SIGNAL_EXTRACTION_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class CerebrasSignalExtractor:
    """Extract signals via Cerebras chat completions, rotating keys and models."""

    provider_name = "cerebras"

    def __init__(
        self,
        client: Any | None = None,
        client_pool: CerebrasClientPool | None = None,
        rate_limiter: CerebrasModelRateLimiter | None = None,
        models: tuple[str, ...] = CEREBRAS_SIGNAL_MODELS,
        max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
    ):
        self._legacy_client = client
        self._pool = (
            client_pool
            if client_pool is not None
            else (None if client is not None else get_cerebras_client_pool())
        )
        self._models = models
        self._max_completion_tokens = max_completion_tokens

        if client is not None:
            self._limiters = [rate_limiter or CerebrasModelRateLimiter(models=models)]
        elif self._pool is not None:
            self._limiters = [
                CerebrasModelRateLimiter(models=models) for _ in range(len(self._pool))
            ]
        else:
            self._limiters = []

    @property
    def is_available(self) -> bool:
        return self._legacy_client is not None or (
            self._pool is not None and len(self._pool) > 0
        )

    def extract_signal_json(self, message: str) -> dict[str, Any]:
        if not self.is_available:
            raise RuntimeError(
                "CEREBRAS_API_KEY or CEREBRAS_API_KEYS is not set — "
                "cannot use Cerebras AI provider"
            )
        if self._legacy_client is not None:
            return self._extract_single_client(self._legacy_client, message)
        return self._extract_with_pool(message)

    def _extract_single_client(self, client: Any, message: str) -> dict[str, Any]:
        limiter = self._limiters[0]
        estimated_tokens = CerebrasModelRateLimiter.estimate_tokens(
            message, self._max_completion_tokens
        )
        last_error: Exception | None = None
        tried: set[str] = set()

        while len(tried) < len(self._models):
            model = limiter.acquire_model(estimated_tokens, exclude=tried)
            tried.add(model)
            try:
                return self._call_model(client, model, message)
            except Exception as exc:
                last_error = exc
                if _is_rate_limit_error(exc):
                    logger.warning(
                        "Cerebras model %s rate-limited (%s) — trying next model",
                        model, exc,
                    )
                    limiter.mark_rate_limited(model)
                    continue
                raise

        raise RuntimeError(
            f"All Cerebras models failed after rate-limit rotation: {last_error}"
        ) from last_error

    def _extract_with_pool(self, message: str) -> dict[str, Any]:
        pool = self._pool
        assert pool is not None

        estimated_tokens = CerebrasModelRateLimiter.estimate_tokens(
            message, self._max_completion_tokens
        )
        tried: set[tuple[int, str]] = set()
        max_attempts = len(pool) * len(self._models)
        last_error: Exception | None = None

        while len(tried) < max_attempts:
            key_idx = pool.acquire_key_index(exclude={k for k, _ in tried})
            limiter = self._limiters[key_idx]
            model = limiter.acquire_model(
                estimated_tokens,
                exclude={m for k, m in tried if k == key_idx},
            )
            tried.add((key_idx, model))

            try:
                return self._call_model(pool.client(key_idx), model, message)
            except Exception as exc:
                last_error = exc
                if _is_rate_limit_error(exc):
                    logger.warning(
                        "Cerebras key #%s model %s rate-limited (%s) — rotating",
                        key_idx + 1, model, exc,
                    )
                    pool.mark_rate_limited(key_idx)
                    limiter.mark_rate_limited(model)
                    continue
                raise

        raise RuntimeError(
            f"All Cerebras API keys and models failed after rotation: {last_error}"
        ) from last_error

    def _call_model(self, client: Any, model: str, message: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "max_completion_tokens": self._max_completion_tokens,
            "messages": [
                {"role": "system", "content": SIGNAL_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
        }
        if model == "gpt-oss-120b":
            kwargs["reasoning_effort"] = "medium"

        completion = client.chat.completions.create(**kwargs)
        if not completion.choices:
            raise ValueError(f"Cerebras {model} returned no choices")
        content = completion.choices[0].message.content
        if not content or not str(content).strip():
            raise ValueError(f"Cerebras {model} returned empty content")
        response_text = str(content).strip()
        logger.debug("Cerebras %s extracted signal JSON (%d chars)", model, len(response_text))
        return parse_ai_json(response_text)


def _is_rate_limit_error(exc: Exception) -> bool:
    name = type(exc).__name__
    if name == "RateLimitError":
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    return "429" in str(exc).lower() or "rate limit" in str(exc).lower()
