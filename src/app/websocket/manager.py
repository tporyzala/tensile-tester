from __future__ import annotations

import json
from collections.abc import Iterable

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, payload: dict[str, object]) -> None:
        stale: list[WebSocket] = []
        message = json.dumps(payload)
        for websocket in self._iter_connections():
            try:
                await websocket.send_text(message)
            except RuntimeError:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(websocket)

    def _iter_connections(self) -> Iterable[WebSocket]:
        return tuple(self._connections)

