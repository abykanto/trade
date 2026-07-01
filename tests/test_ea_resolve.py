"""Tests for EABridge resolve_pending_order_outcome (Python logic + EA snapshots)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.execution.ea_bridge import EABridge
from src.execution.ea_server import EAServer
from src.execution.protocol import RSP_OK, decode_message, encode_message


async def _mock_ea_resolve_client(host: str, port: int) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(encode_message({"type": "CONNECTED", "magic": 234000}))
    await writer.drain()

    while True:
        line = await reader.readline()
        if not line:
            break
        msg = decode_message(line)
        cmd = msg.get("type")
        req_id = msg.get("id")

        if cmd == "PING":
            writer.write(encode_message({"type": RSP_OK, "id": req_id}))
        elif cmd == "GET_ORDERS":
            writer.write(encode_message({
                "type": RSP_OK, "id": req_id, "orders": [],
            }))
        elif cmd == "GET_POSITIONS":
            writer.write(encode_message({
                "type": RSP_OK, "id": req_id,
                "positions": [{
                    "ticket": 9001, "identifier": 9001, "magic": 234000,
                    "volume": 0.05, "price_open": 4360.0,
                    "sl": 4359.0, "tp": 4370.0, "type": 0, "symbol": "XAUUSD",
                }],
            }))
        elif cmd == "GET_ORDER_HISTORY":
            writer.write(encode_message({
                "type": RSP_OK, "id": req_id,
                "history_order": {"ticket": 77701, "state": 4},
                "deals": [{
                    "ticket": 1, "price": 4360.0, "entry": 0,
                    "position_id": 9001, "time": 1700000000,
                }],
            }))
        else:
            writer.write(encode_message({
                "type": "ERR", "id": req_id, "error": f"unmocked {cmd}",
            }))
        await writer.drain()


async def _run_resolve_test() -> None:
    server = EAServer(host="127.0.0.1", port=0, connect_timeout=5.0, request_timeout=5.0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    client_task = asyncio.create_task(_mock_ea_resolve_client("127.0.0.1", port))

    bridge = EABridge(host="127.0.0.1", port=port)
    bridge._server = server
    assert await bridge.connect()

    outcome = await bridge.resolve_pending_order_outcome(
        symbol="XAUUSD",
        order_ticket=77701,
        direction="BUY",
        volume=0.05,
        entry_price=4360.0,
        tick_size=0.01,
    )
    assert outcome.status == "open"
    assert outcome.position_ticket == 9001
    assert outcome.fill_price == 4360.0

    await bridge.shutdown()
    client_task.cancel()
    try:
        await client_task
    except asyncio.CancelledError:
        pass


def test_ea_bridge_resolve_pending_in_python():
    asyncio.run(_run_resolve_test())
