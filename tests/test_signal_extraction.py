from src.signals.extraction import (
    ExtractedSignal,
    parse_signal_deterministic,
    price_in_message,
    process_signal_message,
    validate_extracted_signal,
    validate_signal_geometry,
)


BUY_MESSAGE = """
Pair: XAUUSD
Type: BUY
Entry: 4315
TP1: 4318
TP2: 4320
TP3: 4322
TP4: 4324
TP5: 4326
TP6: 4336
Stop Loss: 4305
"""

SELL_MESSAGE = """
Pair: XAUUSD
Type: SELL
Entry: 4290
TP1: 4285
TP2: 4280
TP3: 4275
TP4: 4270
TP5: 4265
Stop Loss: 4300
"""

FOREX_MESSAGE = """
Pair: EURUSD
Type: BUY
Entry: 1.14920
TP1: 1.14950
TP2: 1.14980
TP3: 1.15000
TP4: 1.15020
TP5: 1.15050
Stop Loss: 1.14850
"""


def test_deterministic_buy_signal():
    signal = parse_signal_deterministic(BUY_MESSAGE)
    assert signal is not None
    assert signal.symbol == "XAUUSD"
    assert signal.direction == "BUY"
    assert signal.entry_price == 4315.0
    assert signal.hard_stop == 4305.0
    assert signal.take_profit == 4326.0
    assert signal.method == "deterministic"


def test_deterministic_sell_signal():
    signal = parse_signal_deterministic(SELL_MESSAGE)
    assert signal is not None
    assert signal.direction == "SELL"
    assert signal.take_profit == 4265.0


def test_deterministic_forex_decimals():
    signal = parse_signal_deterministic(FOREX_MESSAGE)
    assert signal is not None
    assert signal.symbol == "EURUSD"
    assert signal.entry_price == 1.14920
    assert signal.take_profit == 1.15050


def test_tp5_missing_uses_highest_tp_number():
    message = """
    Pair: XAUUSD
    Type: BUY
    Entry: 4315
    TP1: 4318
    TP2: 4320
    TP3: 4322
    TP4: 4324
    TP6: 4336
    Stop Loss: 4305
    """
    signal = parse_signal_deterministic(message)
    assert signal is not None
    assert signal.take_profit == 4336.0


def test_rejects_hallucinated_values():
    fake = ExtractedSignal(
        symbol="XAUUSD",
        direction="BUY",
        entry_price=9999.0,
        hard_stop=4305.0,
        take_profit=4326.0,
        method="ai_verified",
    )
    errors = validate_extracted_signal(fake, BUY_MESSAGE)
    assert any("entry_price=9999.0" in err for err in errors)


def test_rejects_wrong_geometry():
    fake = ExtractedSignal(
        symbol="XAUUSD",
        direction="SELL",
        entry_price=4315.0,
        hard_stop=4305.0,
        take_profit=4326.0,
        method="ai_verified",
    )
    errors = validate_extracted_signal(fake, BUY_MESSAGE)
    assert any("SELL" in err for err in errors)


def test_process_stream_skips_noise():
    result = process_signal_message("Good morning traders")
    assert result.status.value == "skipped"


def test_process_stream_uses_deterministic_without_ai():
    result = process_signal_message(BUY_MESSAGE, ai_provider=None)
    assert result.status.value == "success"
    assert result.signal.method == "deterministic"
    assert result.payload["entry_price"] == 4315.0


def test_ai_fallback_rejects_unverified_output():
    class FakeProvider:
        provider_name = "fake"

        def extract_signal_json(self, _message: str) -> dict:
            return {
                "valid_signal": True,
                "symbol": "XAUUSD",
                "direction": "BUY",
                "entry_price": 9999.0,
                "hard_stop": 4305.0,
                "take_profit": 4326.0,
            }

    messy = """
    XAUUSD long idea
    buy @ 4315
    TP1 4318
    TP2 4320
    TP3 4322
    TP5 4326
    sl 4305
    """
    result = process_signal_message(messy, ai_provider=FakeProvider())
    assert result.status.value == "rejected"
    assert "verification" in result.reason


def test_price_in_message_handles_forex_trailing_zeros():
    assert price_in_message("Entry: 1.14920", 1.1492)
    assert not price_in_message("Entry: 1.14920", 1.15)


def test_validate_signal_geometry_buy_and_sell():
    assert validate_signal_geometry("BUY", 4315.0, 4305.0, 4326.0) is None
    assert validate_signal_geometry("SELL", 4290.0, 4300.0, 4265.0) is None
    assert validate_signal_geometry("BUY", 4315.0, 4316.0, 4326.0) is not None
