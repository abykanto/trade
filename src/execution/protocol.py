"""JSON-line protocol shared by Python and the MQL5 EA executor.

Each message is one JSON object terminated by newline (\\n).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def encode_message(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def decode_message(line: bytes | str) -> dict[str, Any]:
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        raise ValueError("empty protocol line")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("protocol message must be a JSON object")
    return data


# ── Command types (Python → EA) ─────────────────────────────────────────────

CMD_PING = "PING"
CMD_GET_TICK = "GET_TICK"
CMD_GET_SYMBOL_SPEC = "GET_SYMBOL_SPEC"
CMD_GET_ACCOUNT_INFO = "GET_ACCOUNT_INFO"
CMD_GET_POSITIONS = "GET_POSITIONS"
CMD_GET_ORDERS = "GET_ORDERS"
CMD_GET_POSITION = "GET_POSITION"
CMD_PLACE_PENDING = "PLACE_PENDING"
CMD_CANCEL_ORDER = "CANCEL_ORDER"
CMD_MODIFY_POSITION = "MODIFY_POSITION"
CMD_CLOSE_POSITION = "CLOSE_POSITION"
CMD_GET_ORDER_HISTORY = "GET_ORDER_HISTORY"
CMD_GET_CLOSE_DETAILS = "GET_CLOSE_DETAILS"

# ── Response / event types (EA → Python) ────────────────────────────────────

RSP_OK = "OK"
RSP_ERR = "ERR"
EVT_TRADE = "TRADE_EVENT"
EVT_HEARTBEAT = "HEARTBEAT"
EVT_CONNECTED = "CONNECTED"


@dataclass
class EARequest:
    cmd: str
    params: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=new_request_id)

    def to_dict(self) -> dict[str, Any]:
        payload = {"type": self.cmd, "id": self.request_id}
        payload.update(self.params)
        return payload


@dataclass
class EAResponse:
    request_id: str | None
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    retcode: int | None = None

    @classmethod
    def from_dict(cls, msg: dict[str, Any]) -> EAResponse:
        msg_type = msg.get("type", "")
        if msg_type == RSP_OK:
            return cls(
                request_id=msg.get("id"),
                ok=True,
                data={k: v for k, v in msg.items() if k not in ("type", "id")},
                retcode=msg.get("retcode"),
            )
        if msg_type == RSP_ERR:
            return cls(
                request_id=msg.get("id"),
                ok=False,
                error=msg.get("error") or msg.get("comment") or "unknown error",
                retcode=msg.get("retcode"),
                data=msg,
            )
        return cls(request_id=msg.get("id"), ok=False, error=f"unexpected type: {msg_type}")
