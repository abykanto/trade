"""Regex-based Telegram signal parsing (no AI)."""

from __future__ import annotations

import re
from typing import Optional

from src.ai.text_signals.models import ExtractedSignal
from src.ai.text_signals.validation import validate_extracted_signal

REQUIRED_TP_MARKERS = ("TP1", "TP2", "TP3")

SYMBOL_RE = re.compile(
    r"(?:Pair|Symbol|Instrument)\s*[:#]?\s*([A-Z]{3,12})",
    re.IGNORECASE,
)
DIRECTION_RE = re.compile(
    r"(?:Type|Direction|Side)\s*[:#]?\s*(BUY|SELL)",
    re.IGNORECASE,
)
ENTRY_RE = re.compile(r"Entry\s*[:#]?\s*([\d.]+)", re.IGNORECASE)
STOP_RE = re.compile(
    r"(?:Stop\s*Loss|SL|Stop)\s*[:#]?\s*([\d.]+)",
    re.IGNORECASE,
)
TP_RE = re.compile(r"TP\s*(\d+)\s*[:#]?\s*([\d.]+)", re.IGNORECASE)


def is_candidate_signal(message: str) -> bool:
    upper = message.upper()
    return all(token in upper for token in REQUIRED_TP_MARKERS)


def _parse_float(raw: str) -> float:
    return float(raw.strip())


def _select_take_profit(tps: dict[int, float]) -> Optional[float]:
    if not tps:
        return None
    if 5 in tps:
        return tps[5]
    return tps[max(tps.keys())]


def parse_signal_deterministic(message: str) -> Optional[ExtractedSignal]:
    """Parse structured Telegram signals with regex. No model calls."""
    symbol_match = SYMBOL_RE.search(message)
    direction_match = DIRECTION_RE.search(message)
    entry_match = ENTRY_RE.search(message)
    stop_match = STOP_RE.search(message)

    if not all([symbol_match, direction_match, entry_match, stop_match]):
        return None

    tps: dict[int, float] = {}
    for tp_match in TP_RE.finditer(message):
        tps[int(tp_match.group(1))] = _parse_float(tp_match.group(2))

    take_profit = _select_take_profit(tps)
    if take_profit is None:
        return None

    signal = ExtractedSignal(
        symbol=symbol_match.group(1).upper(),
        direction=direction_match.group(1).upper(),
        entry_price=_parse_float(entry_match.group(1)),
        hard_stop=_parse_float(stop_match.group(1)),
        take_profit=take_profit,
        method="deterministic",
    )

    if validate_extracted_signal(signal, message):
        return None

    return signal
