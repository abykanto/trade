"""Async TCP server that accepts a connection from the MQL5 EA executor."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Awaitable

from src.execution.protocol import (
    CMD_PING,
    EVT_CONNECTED,
    EVT_HEARTBEAT,
    EVT_TRADE,
    EARequest,
    EAResponse,
    RSP_ERR,
    RSP_OK,
    decode_message,
    encode_message,
)

logger = logging.getLogger(__name__)


class EAServer:
    """Listens for the EA; correlates request/response pairs by id."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 19520,
        connect_timeout: float = 120.0,
        request_timeout: float = 30.0,
    ):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout

        self._server: asyncio.AbstractServer | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._pending: dict[str, asyncio.Future] = {}
        self._trade_events: deque[dict[str, Any]] = deque(maxlen=500)
        self._on_trade_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    def set_trade_event_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        self._on_trade_event = handler

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("EA server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._connected.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("EA server stopped"))
        self._pending.clear()

    async def wait_for_ea(self, timeout: float | None = None) -> bool:
        timeout = timeout if timeout is not None else self.connect_timeout
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def request(self, req: EARequest) -> EAResponse:
        if not self.is_connected or self._writer is None:
            return EAResponse(request_id=req.request_id, ok=False, error="EA not connected")

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req.request_id] = fut

        try:
            async with self._lock:
                self._writer.write(encode_message(req.to_dict()))
                await self._writer.drain()
            raw = await asyncio.wait_for(fut, timeout=self.request_timeout)
            return EAResponse.from_dict(raw)
        except asyncio.TimeoutError:
            self._pending.pop(req.request_id, None)
            return EAResponse(
                request_id=req.request_id, ok=False, error="EA request timeout"
            )
        except Exception as exc:
            self._pending.pop(req.request_id, None)
            return EAResponse(request_id=req.request_id, ok=False, error=str(exc))

    async def ping(self) -> bool:
        rsp = await self.request(EARequest(cmd=CMD_PING))
        return rsp.ok

    def pop_trade_events(self) -> list[dict[str, Any]]:
        events = list(self._trade_events)
        self._trade_events.clear()
        return events

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("EA connected from %s", peer)

        if self._writer and not self._writer.is_closing():
            logger.warning("Replacing existing EA connection with %s", peer)
            await self._drop_client()

        self._reader = reader
        self._writer = writer
        self._connected.set()
        self._read_task = asyncio.create_task(self._read_loop())

    async def _drop_client(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        self._connected.clear()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        buffer = b""
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    await self._dispatch_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("EA read loop error: %s", exc)
        finally:
            logger.warning("EA disconnected")
            await self._drop_client()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("EA disconnected"))
            self._pending.clear()

    async def _dispatch_line(self, line: bytes) -> None:
        try:
            msg = decode_message(line)
        except Exception as exc:
            logger.warning("Invalid EA message: %s (%s)", line[:200], exc)
            return

        msg_type = msg.get("type", "")
        request_id = msg.get("id")

        if msg_type in (RSP_OK, RSP_ERR) and request_id:
            fut = self._pending.pop(request_id, None)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        if msg_type == EVT_CONNECTED:
            logger.info(
                "EA ready: magic=%s terminal=%s",
                msg.get("magic"),
                msg.get("terminal"),
            )
            return

        if msg_type == EVT_HEARTBEAT:
            return

        if msg_type == EVT_TRADE:
            self._trade_events.append(msg)
            if self._on_trade_event:
                try:
                    await self._on_trade_event(msg)
                except Exception as exc:
                    logger.error("trade event handler failed: %s", exc)
            return

        logger.debug("Unhandled EA message: %s", msg)
