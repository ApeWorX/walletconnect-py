"""
In-process WalletConnect-shaped fake relay for offline tests.

Accepts any ``?auth=&projectId=`` WebSocket URL, speaks ``irn_*`` JSON-RPC, and
delivers published messages to subscribers on the same topic.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from walletconnect.utils.ids import payload_id


@dataclass
class _ClientState:
    ws: Any
    subscriptions: dict[str, str] = field(default_factory=dict)  # topic -> sub_id
    sub_ids: dict[str, str] = field(default_factory=dict)  # sub_id -> topic


class FakeRelay:
    def __init__(self) -> None:
        self.clients: list[_ClientState] = []
        self.mailbox: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._sub_counter = 0

    def _next_sub_id(self) -> str:
        self._sub_counter += 1
        return f"sub-{self._sub_counter}"

    async def handler(self, websocket: Any) -> None:
        # websockets>=13 passes the connection; query is on request.path / request
        request = getattr(websocket, "request", None)
        path = getattr(request, "path", None) or getattr(websocket, "path", "/")
        query = parse_qs(urlparse(path).query)
        if "projectId" not in query or "auth" not in query:
            await websocket.close(4001, "missing auth or projectId")
            return

        state = _ClientState(ws=websocket)
        self.clients.append(state)
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                message = json.loads(raw)
                await self._dispatch(state, message)
        finally:
            if state in self.clients:
                self.clients.remove(state)

    async def _dispatch(self, state: _ClientState, message: dict[str, Any]) -> None:
        if "method" not in message:
            # client acks — ignore
            return
        method = message["method"]
        request_id = message["id"]
        params = message.get("params") or {}

        if method == "irn_publish":
            await self._publish(params)
            await state.ws.send(json.dumps({"id": request_id, "jsonrpc": "2.0", "result": True}))
        elif method == "irn_subscribe":
            topic = params["topic"]
            sub_id = self._next_sub_id()
            state.subscriptions[topic] = sub_id
            state.sub_ids[sub_id] = topic
            await state.ws.send(json.dumps({"id": request_id, "jsonrpc": "2.0", "result": sub_id}))
            # Deliver any mailbox messages after subscribe.
            for item in list(self.mailbox.get(topic, [])):
                await self._deliver(state, sub_id, item)
        elif method == "irn_unsubscribe":
            topic = params["topic"]
            sub_id = params["id"]
            state.subscriptions.pop(topic, None)
            state.sub_ids.pop(sub_id, None)
            await state.ws.send(json.dumps({"id": request_id, "jsonrpc": "2.0", "result": True}))
        elif method == "irn_fetchMessages":
            topic = params["topic"]
            messages = list(self.mailbox.get(topic, []))
            await state.ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "jsonrpc": "2.0",
                        "result": {"messages": messages, "hasMore": False},
                    }
                )
            )
        else:
            await state.ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "jsonrpc": "2.0",
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    }
                )
            )

    async def _publish(self, params: dict[str, Any]) -> None:
        topic = params["topic"]
        item = {
            "topic": topic,
            "message": params["message"],
            "publishedAt": int(time.time() * 1000),
            "tag": params["tag"],
            "attestation": params.get("attestation"),
        }
        # Always store briefly so fetchMessages works.
        self.mailbox[topic].append(item)
        for client in list(self.clients):
            sub_id = client.subscriptions.get(topic)
            if sub_id is not None:
                await self._deliver(client, sub_id, item)

    async def _deliver(self, state: _ClientState, sub_id: str, item: dict[str, Any]) -> None:
        request = {
            "id": payload_id(),
            "jsonrpc": "2.0",
            "method": "irn_subscription",
            "params": {"id": sub_id, "data": item},
        }
        await state.ws.send(json.dumps(request))


@asynccontextmanager
async def run_fake_relay(host: str = "127.0.0.1") -> AsyncIterator[tuple[FakeRelay, str]]:
    """Yield ``(fake, ws_url)`` where ``ws_url`` is ``ws://host:port`` (no query)."""
    import websockets

    fake = FakeRelay()
    async with websockets.serve(fake.handler, host, 0) as server:
        sock_list = list(server.sockets or [])
        port = sock_list[0].getsockname()[1]
        yield fake, f"ws://{host}:{port}"
