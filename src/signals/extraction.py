"""Backward-compatible re-exports — prefer ``src.ai.text_signals``."""

from src.ai.text_signals import (
    ExtractedSignal,
    ProcessResult,
    ProcessStatus,
    build_payload,
    is_candidate_signal,
    parse_signal_deterministic,
    payload_to_curl,
    price_in_message,
    prices_match,
    process_signal_message,
    signals_equivalent,
    validate_extracted_signal,
    validate_signal_geometry,
)
from src.ai.text_signals.processor import parse_ai_json, signal_from_ai_dict

__all__ = [
    "ExtractedSignal",
    "ProcessResult",
    "ProcessStatus",
    "build_payload",
    "is_candidate_signal",
    "parse_ai_json",
    "parse_signal_deterministic",
    "payload_to_curl",
    "price_in_message",
    "prices_match",
    "process_signal_message",
    "signal_from_ai_dict",
    "signals_equivalent",
    "validate_extracted_signal",
    "validate_signal_geometry",
]
