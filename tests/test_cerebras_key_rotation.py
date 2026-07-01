"""Tests for Cerebras API key pool rotation."""

from src.ai.providers.cerebras.client import CerebrasClientPool
from src.ai.providers.cerebras.extractor import CerebrasSignalExtractor
from src.ai.providers.cerebras.keys import load_cerebras_api_keys
from src.ai.providers.cerebras.rate_limiter import CerebrasModelRateLimiter


class _RateLimitError(Exception):
    pass


class _FakeCompletions:
    def __init__(self, key_label: str, fail: bool = False):
        self.key_label = key_label
        self.fail = fail
        self.calls: list[str] = []

    def create(self, **kwargs):
        model = kwargs["model"]
        self.calls.append(model)
        if self.fail:
            raise _RateLimitError("429 rate limit")

        class Message:
            content = (
                '{"valid_signal": true, "symbol": "XAUUSD", "direction": "BUY", '
                '"entry_price": 4315.0, "hard_stop": 4305.0, "take_profit": 4326.0}'
            )

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]

        return Response()


class _FakeClient:
    def __init__(self, label: str, fail: bool = False):
        self.label = label
        self.chat = type("Chat", (), {
            "completions": _FakeCompletions(label, fail=fail),
        })()


def test_load_api_keys_dedupes(monkeypatch):
    monkeypatch.setenv(
        "CEREBRAS_API_KEYS",
        "key-a,key-b",
    )
    monkeypatch.setenv("CEREBRAS_API_KEY", "key-a")
    assert load_cerebras_api_keys() == ("key-a", "key-b")


def test_client_pool_rotates_on_rate_limit():
    pool = CerebrasClientPool(("k1", "k2"))
    pool._clients = [_FakeClient("k1", fail=True), _FakeClient("k2")]  # noqa: SLF001

    limiters = [
        CerebrasModelRateLimiter(models=("gemma-4-31b",)),
        CerebrasModelRateLimiter(models=("gemma-4-31b",)),
    ]
    extractor = CerebrasSignalExtractor(
        client_pool=pool,
        rate_limiter=limiters[0],
    )
    extractor._limiters = limiters  # noqa: SLF001

    data = extractor.extract_signal_json("test")
    assert data["valid_signal"] is True
    assert pool._clients[0].chat.completions.fail  # noqa: SLF001
    assert pool._clients[1].chat.completions.calls == ["gemma-4-31b"]


def test_acquire_key_index_round_robin():
    pool = CerebrasClientPool(("a", "b"))
    first = pool.acquire_key_index()
    second = pool.acquire_key_index()
    assert (first, second) in {(0, 1), (1, 0)}
    assert first != second
