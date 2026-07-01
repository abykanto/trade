"""Tests for Cerebras multi-model rate limiter."""

import time

import pytest

from src.ai.providers.cerebras.rate_limiter import CerebrasModelRateLimiter


def test_rate_limiter_rotates_across_models():
    limiter = CerebrasModelRateLimiter(
        models=("model-a", "model-b", "model-c"),
    )
    picked = [limiter.acquire_model(1000) for _ in range(3)]
    assert len(set(picked)) == 3


def test_rate_limiter_enforces_min_interval_per_model():
    limiter = CerebrasModelRateLimiter(models=("only-model",))
    limiter.acquire_model(500)
    with pytest.raises(RuntimeError, match="rate-limited"):
        limiter.acquire_model(500, max_wait_sec=0.05)


def test_rate_limiter_exclude_skips_rate_limited_model():
    limiter = CerebrasModelRateLimiter(models=("model-a", "model-b"))
    limiter.mark_rate_limited("model-a", cooldown_sec=60.0)
    model = limiter.acquire_model(500, exclude=set())
    assert model == "model-b"


def test_estimate_tokens_includes_completion_budget():
    tokens = CerebrasModelRateLimiter.estimate_tokens("hello world", 512)
    assert tokens >= 512
