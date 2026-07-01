"""Anti-hallucination checks and level geometry validation."""

from __future__ import annotations

import re
from typing import Optional

from src.ai.text_signals.models import ExtractedSignal

NUMBER_RE = re.compile(r"\d+\.\d+|\d+")


def numbers_in_message(message: str) -> list[float]:
    values: list[float] = []
    for raw in NUMBER_RE.findall(message):
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


def prices_match(expected: float, actual: float) -> bool:
    if expected == actual:
        return True
    if abs(expected - actual) <= max(1e-6, abs(expected) * 1e-5):
        return True
    expected_text = f"{expected:.10f}".rstrip("0").rstrip(".")
    actual_text = f"{actual:.10f}".rstrip("0").rstrip(".")
    return expected_text == actual_text


def price_in_message(message: str, price: float) -> bool:
    return any(prices_match(price, found) for found in numbers_in_message(message))


def validate_signal_geometry(
    direction: str,
    entry: float,
    hard_stop: float,
    take_profit: float,
) -> Optional[str]:
    direction = direction.upper()
    if direction == "BUY":
        if hard_stop >= entry:
            return (
                f"BUY: hard_stop must be below entry "
                f"(entry={entry}, hard_stop={hard_stop})"
            )
        if take_profit <= entry:
            return (
                f"BUY: take_profit must be above entry "
                f"(entry={entry}, take_profit={take_profit})"
            )
        return None

    if direction != "SELL":
        return f"invalid direction: {direction}"

    if hard_stop < entry and take_profit > entry:
        return "levels match BUY but direction is SELL"
    if hard_stop <= entry:
        return (
            f"SELL: hard_stop must be above entry "
            f"(entry={entry}, hard_stop={hard_stop})"
        )
    if take_profit >= entry:
        return (
            f"SELL: take_profit must be below entry "
            f"(entry={entry}, take_profit={take_profit})"
        )
    return None


def validate_extracted_signal(
    signal: ExtractedSignal,
    message: str,
) -> list[str]:
    """Ensure every field is grounded in the source message."""
    errors: list[str] = []

    if signal.symbol.upper() not in message.upper():
        errors.append(f"symbol {signal.symbol!r} not found in message")

    if signal.direction.upper() not in message.upper():
        errors.append(f"direction {signal.direction!r} not found in message")

    for field_name, value in (
        ("entry_price", signal.entry_price),
        ("hard_stop", signal.hard_stop),
        ("take_profit", signal.take_profit),
    ):
        if not price_in_message(message, value):
            errors.append(f"{field_name}={value} not found in message text")

    geometry_error = validate_signal_geometry(
        signal.direction,
        signal.entry_price,
        signal.hard_stop,
        signal.take_profit,
    )
    if geometry_error:
        errors.append(geometry_error)

    return errors


def signals_equivalent(a: ExtractedSignal, b: ExtractedSignal) -> bool:
    return (
        a.symbol == b.symbol
        and a.direction == b.direction
        and prices_match(a.entry_price, b.entry_price)
        and prices_match(a.hard_stop, b.hard_stop)
        and prices_match(a.take_profit, b.take_profit)
    )
