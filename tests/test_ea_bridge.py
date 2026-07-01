"""Tests for EABridge against a mock EA TCP client."""

from __future__ import annotations

import asyncio

from src.execution.ea_bridge import EABridge, TRADE_RETCODE_DONE
from src.execution.ea_server import EAServer
from src.execution.protocol import RSP_OK, decode_message, encode_message


async def _mock_ea_client(host: str, port: int) -> None:
    """Simulate EA: connect, answer PING and GET_TICK."""
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
        elif cmd == "GET_TICK":
            writer.write(encode_message({
                "type": RSP_OK, "id": req_id, "bid": 4355.0, "ask": 4355.2,
            }))
        elif cmd == "PLACE_PENDING":
            writer.write(encode_message({
                "type": RSP_OK, "id": req_id,
                "retcode": TRADE_RETCODE_DONE, "order": 77701,
            }))
        else:
            writer.write(encode_message({
                "type": "ERR", "id": req_id, "error": f"unmocked {cmd}",
            }))
        await writer.drain()


async def _run_bridge_test() -> None:
    server = EAServer(host="127.0.0.1", port=0, connect_timeout=5.0, request_timeout=5.0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    client_task = asyncio.create_task(_mock_ea_client("127.0.0.1", port))

    bridge = EABridge(host="127.0.0.1", port=port)
    bridge._server = server

    assert await bridge.connect()
    tick = await bridge.get_tick("XAUUSD")
    assert tick is not None
    assert tick.bid == 4355.0
    assert tick.ask == 4355.2

    result = await bridge.place_pending_order(
        symbol="XAUUSD", direction="BUY", volume=0.05,
        entry_price=4360.0, bid=4355.0, ask=4355.2,
        sl=4359.0, tp=4370.0, tick_size=0.01,
    )
    assert result is not None
    assert result.retcode == TRADE_RETCODE_DONE
    assert result.order == 77701

    await bridge.shutdown()
    client_task.cancel()
    try:
        await client_task
    except asyncio.CancelledError:
        pass


def test_ea_bridge_connect_and_get_tick():
    asyncio.run(_run_bridge_test())
