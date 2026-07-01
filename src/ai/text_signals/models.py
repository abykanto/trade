"""Shared types for text-message signal processing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class ProcessStatus(str, Enum):
    SKIPPED = "skipped"
    REJECTED = "rejected"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class ExtractedSignal:
    symbol: str
    direction: str
    entry_price: float
    hard_stop: float
    take_profit: float
    method: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "hard_stop": self.hard_stop,
            "take_profit": self.take_profit,
        }


@dataclass(frozen=True)
class ProcessResult:
    status: ProcessStatus
    message: str
    signal: Optional[ExtractedSignal] = None
    payload: Optional[dict[str, Any]] = None
    reason: Optional[str] = None
