"""Tests for Cerebras signal extractor failover."""

from src.ai.providers.cerebras.extractor import CerebrasSignalExtractor
from src.ai.providers.cerebras.rate_limiter import CerebrasModelRateLimiter
from src.ai.text_signals import process_signal_message


class _RateLimitError(Exception):
    pass


class _FakeCompletions:
    def __init__(self, responses: dict[str, str], fail_models: set[str]):
        self._responses = responses
        self._fail_models = fail_models
        self.calls: list[str] = []

    def create(self, **kwargs):
        model = kwargs["model"]
        self.calls.append(model)
        if model in self._fail_models:
            raise _RateLimitError("429 rate limit")
        content = self._responses[model]

        class Message:
            pass

        class Choice:
            pass

        class Response:
            choices = [Choice()]

        Response.choices[0].message = Message()
        Response.choices[0].message.content = content
        return Response()


class _FakeClient:
    def __init__(self, completions: _FakeCompletions):
        self.chat = type("Chat", (), {"completions": completions})()


def test_extractor_fails_over_on_rate_limit():
    good_json = (
        '{"valid_signal": true, "symbol": "XAUUSD", "direction": "BUY", '
        '"entry_price": 4315.0, "hard_stop": 4305.0, "take_profit": 4326.0}'
    )
    completions = _FakeCompletions(
        {"gemma-4-31b": good_json, "gpt-oss-120b": good_json, "zai-glm-4.7": good_json},
        fail_models={"gemma-4-31b"},
    )
    limiter = CerebrasModelRateLimiter(
        models=("gemma-4-31b", "gpt-oss-120b", "zai-glm-4.7"),
    )
    extractor = CerebrasSignalExtractor(
        client=_FakeClient(completions),
        rate_limiter=limiter,
    )

    data = extractor.extract_signal_json("test message")
    assert data["valid_signal"] is True
    assert "gemma-4-31b" in completions.calls
    assert any(m != "gemma-4-31b" for m in completions.calls)


def test_process_message_uses_ai_provider():
    good_json = (
        '{"valid_signal": true, "symbol": "XAUUSD", "direction": "BUY", '
        '"entry_price": 4315.0, "hard_stop": 4305.0, "take_profit": 4326.0}'
    )
    completions = _FakeCompletions(
        {"gemma-4-31b": good_json, "gpt-oss-120b": good_json, "zai-glm-4.7": good_json},
        fail_models=set(),
    )
    extractor = CerebrasSignalExtractor(
        client=_FakeClient(completions),
        rate_limiter=CerebrasModelRateLimiter(
            models=("gemma-4-31b", "gpt-oss-120b", "zai-glm-4.7"),
        ),
    )
    messy = """
    XAUUSD long idea
    buy @ 4315
    TP1 4318
    TP2 4320
    TP3 4322
    TP5 4326
    sl 4305
    """
    result = process_signal_message(messy, ai_provider=extractor)
    assert result.status.value == "success"
    assert result.signal.method.startswith("ai_verified:cerebras")
