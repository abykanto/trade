"""Text-message signal processing (provider-agnostic)."""

from src.ai.text_signals.deterministic import (
    is_candidate_signal,
    parse_signal_deterministic,
)
from src.ai.text_signals.models import ExtractedSignal, ProcessResult, ProcessStatus
from src.ai.text_signals.payload import build_payload, payload_to_curl
from src.ai.text_signals.processor import process_signal_message
from src.ai.text_signals.validation import (
    price_in_message,
    prices_match,
    signals_equivalent,
    validate_extracted_signal,
    validate_signal_geometry,
)

__all__ = [
    "ExtractedSignal",
    "ProcessResult",
    "ProcessStatus",
    "build_payload",
    "is_candidate_signal",
    "parse_signal_deterministic",
    "payload_to_curl",
    "price_in_message",
    "prices_match",
    "process_signal_message",
    "signals_equivalent",
    "validate_extracted_signal",
    "validate_signal_geometry",
]
