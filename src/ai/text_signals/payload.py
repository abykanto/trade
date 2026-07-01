"""Build API payloads and curl commands from extracted signals."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.ai.text_signals.models import ExtractedSignal


def build_payload(
    signal: ExtractedSignal,
    *,
    max_idea_risk: float = 2.5,
    lot_size: float = 0.03,
) -> dict[str, Any]:
    return {
        "symbol": signal.symbol,
        "direction": signal.direction,
        "entry_price": signal.entry_price,
        "hard_stop": signal.hard_stop,
        "take_profit": signal.take_profit,
        "max_idea_risk": max_idea_risk,
        "lot_size": lot_size,
        "source": (
            f"{signal.symbol.lower()}_"
            f"{signal.direction.lower()}_"
            f"{datetime.now():%Y-%m-%dT%H-%M-%S}"
        ),
    }


def payload_to_curl(payload: dict[str, Any], url: str) -> str:
    payload_json = json.dumps(payload, indent=2)
    return (
        f"curl -X POST {url} "
        "-H 'Content-Type: application/json' "
        f"-d '{payload_json}'"
    )
