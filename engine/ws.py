"""WebSocket connection manager for real-time event streaming."""

import asyncio
import json
from decimal import Decimal
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
import structlog

logger = structlog.get_logger()


class _DecimalEncoder(json.JSONEncoder):
    """JSON encoder that converts Decimal to float for WebSocket messages."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


class ConnectionManager:
    """Manages WebSocket connections and broadcasts events to all clients."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def client_count(self) -> int:
        return len(self._connections)

    async def accept(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        logger.info("ws_client_connected", clients=self.client_count)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        logger.info("ws_client_disconnected", clients=self.client_count)

    def broadcast(self, event: dict) -> None:
        """Broadcast an event dict to all connected clients.

        Safe to call from both sync and async contexts — the scheduler
        calls this synchronously from APScheduler job threads.
        """
        if not self._connections:
            return

        payload = json.dumps(event, cls=_DecimalEncoder)

        # If we're already in the event loop, schedule coroutines directly.
        # Otherwise fire-and-forget from a different thread.
        loop = self._loop
        if loop is None:
            return

        try:
            if loop.is_running():
                loop.create_task(self._send_all(payload))
            else:
                asyncio.run_coroutine_threadsafe(self._send_all(payload), loop)
        except RuntimeError:
            pass  # loop closed during shutdown

    async def _send_all(self, payload: str) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    async def handle(self, ws: WebSocket) -> None:
        """Full lifecycle handler for a WebSocket connection."""
        await self.accept(ws)
        try:
            while True:
                # We don't expect client messages, but must read to detect close
                await ws.receive_text()
        except WebSocketDisconnect:
            self.disconnect(ws)
        except Exception:
            self.disconnect(ws)


# Singleton — imported by main.py and used app-wide
ws_manager = ConnectionManager()
