"""Orchestrate deterministic parsing, optional AI fallback, and verification."""

from __future__ import annotations

import json
import re
from typing import Any

from src.ai.protocol import SignalAIProvider
from src.ai.text_signals.deterministic import is_candidate_signal, parse_signal_deterministic
from src.ai.text_signals.models import ExtractedSignal, ProcessResult, ProcessStatus
from src.ai.text_signals.payload import build_payload
from src.ai.text_signals.validation import validate_extracted_signal


def parse_ai_json(response_text: str) -> dict[str, Any]:
    text = response_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def signal_from_ai_dict(data: dict[str, Any], *, provider: str) -> ExtractedSignal:
    return ExtractedSignal(
        symbol=str(data["symbol"]).upper(),
        direction=str(data["direction"]).upper(),
        entry_price=float(data["entry_price"]),
        hard_stop=float(data["hard_stop"]),
        take_profit=float(data["take_profit"]),
        method=f"ai_verified:{provider}",
    )


def process_signal_message(
    message: str,
    *,
    ai_provider: SignalAIProvider | None = None,
    max_idea_risk: float = 2.5,
    lot_size: float = 0.03,
) -> ProcessResult:
    """
    Process one message from a stream.

    1. Skip noise (missing TP markers).
    2. Prefer deterministic regex extraction (no hallucination risk).
    3. Fall back to AI only when regex cannot parse.
    4. Reject AI output unless every value is verified against the message.
    """
    if not is_candidate_signal(message):
        return ProcessResult(
            status=ProcessStatus.SKIPPED,
            message=message,
            reason="missing TP1/TP2/TP3 markers",
        )

    deterministic = parse_signal_deterministic(message)
    if deterministic is not None:
        payload = build_payload(
            deterministic,
            max_idea_risk=max_idea_risk,
            lot_size=lot_size,
        )
        return ProcessResult(
            status=ProcessStatus.SUCCESS,
            message=message,
            signal=deterministic,
            payload=payload,
        )

    if ai_provider is None:
        return ProcessResult(
            status=ProcessStatus.REJECTED,
            message=message,
            reason="structured parse failed and no AI provider configured",
        )

    try:
        ai_data = ai_provider.extract_signal_json(message)
    except Exception as exc:
        return ProcessResult(
            status=ProcessStatus.FAILED,
            message=message,
            reason=str(exc),
        )

    if not ai_data.get("valid_signal"):
        return ProcessResult(
            status=ProcessStatus.REJECTED,
            message=message,
            reason=str(ai_data.get("reason", "invalid signal")),
        )

    try:
        ai_signal = signal_from_ai_dict(ai_data, provider=ai_provider.provider_name)
    except (KeyError, TypeError, ValueError) as exc:
        return ProcessResult(
            status=ProcessStatus.REJECTED,
            message=message,
            reason=f"malformed AI response: {exc}",
        )

    verification_errors = validate_extracted_signal(ai_signal, message)
    if verification_errors:
        return ProcessResult(
            status=ProcessStatus.REJECTED,
            message=message,
            reason="AI output failed verification: " + "; ".join(verification_errors),
        )

    payload = build_payload(
        ai_signal,
        max_idea_risk=max_idea_risk,
        lot_size=lot_size,
    )
    return ProcessResult(
        status=ProcessStatus.SUCCESS,
        message=message,
        signal=ai_signal,
        payload=payload,
    )
