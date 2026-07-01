"""Tests for EA TRADE_EVENT handler wiring."""

from __future__ import annotations

import asyncio

from src.execution.ea_server import EAServer
from src.execution.protocol import EVT_TRADE, encode_message


async def _run_event_test() -> None:
    received: list[dict] = []

    async def handler(msg: dict) -> None:
        received.append(msg)

    server = EAServer(host="127.0.0.1", port=0)
    server.set_trade_event_handler(handler)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(encode_message({"type": "CONNECTED", "magic": 234000}))
    writer.write(encode_message({
        "type": EVT_TRADE,
        "event": "POSITION_OPENED",
        "ticket": 123,
        "symbol": "XAUUSD",
        "price": 4360.0,
        "profit": 0.0,
        "magic": 234000,
    }))
    await writer.drain()
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0]["event"] == "POSITION_OPENED"
    assert received[0]["symbol"] == "XAUUSD"

    writer.close()
    await server.stop()


def test_ea_server_trade_event_handler():
    asyncio.run(_run_event_test())
