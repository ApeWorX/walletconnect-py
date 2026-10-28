"""
Async WebSocket client for the WalletConnect ``irn`` relay.

Connects to ``wss://relay.walletconnect.org`` by default with a did-JWT auth
token and project ID. Unit tests use an in-process fake relay (see
``tests/fake_relay.py``); live tests are opt-in and skip without
``WALLETCONNECT_PROJECT_ID``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from walletconnect.constants import (
    DEFAULT_RELAY_URL,
    PROJECT_ID_ENV_VAR,
    USER_AGENT,
)
from walletconnect.crypto.jwt import AuthKeyPair, generate_auth_key_pair, sign_jwt
from walletconnect.exceptions import (
    MissingProjectIdError,
    RelayConnectionError,
    RelayError,
    WalletConnectTimeoutError,
)

from .rpc import RelayRPCError, make_request, make_result

logger = logging.getLogger(__name__)

MessageHandler = Callable[["RelayMessage"], Awaitable[None] | None]

_DEFAULT_REQUEST_TIMEOUT = 30.0
# Close codes the JS SDK treats as reconnectable.
_RECONNECT_CLOSE_CODES = {1001, 4010}


@dataclass(frozen=True, slots=True)
class RelayMessage:
    """A message delivered by the relay on a subscribed topic."""

    topic: str
    message: str
    published_at: int
    tag: int
    attestation: str | None = None
    subscription_id: str | None = None


class RelayClient:
    """
    Minimal async ``irn`` relay client (dApp / publisher+subscriber side).

    Typical use::

        client = RelayClient(project_id="...")
        await client.connect()
        sub_id = await client.subscribe(topic)
        await client.publish(topic, envelope, ttl=300, tag=1100)
        ...
        await client.close()
    """

    def __init__(
        self,
        project_id: str | None = None,
        *,
        relay_url: str = DEFAULT_RELAY_URL,
        auth_key: AuthKeyPair | None = None,
        origin: str | None = None,
        user_agent: str = USER_AGENT,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        resolved = project_id if project_id is not None else os.environ.get(PROJECT_ID_ENV_VAR)
        if not resolved:
            raise MissingProjectIdError()
        self.project_id = resolved
        self.relay_url = relay_url.rstrip("/")
        self.auth_key = auth_key or generate_auth_key_pair()
        self.origin = origin
        self.user_agent = user_agent
        self.request_timeout = request_timeout

        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: dict[str, str] = {}  # topic -> subscription id
        self._handlers: list[MessageHandler] = []
        self._closed = asyncio.Event()
        self._connected = False

    @property
    def client_id(self) -> str:
        """The persistent ``did:key:…`` presented to the relay."""
        return self.auth_key.did_key

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    def on_message(self, handler: MessageHandler) -> None:
        """Register a callback invoked for every ``irn_subscription`` message."""
        self._handlers.append(handler)

    async def connect(self) -> None:
        """Open the WebSocket and start the reader loop."""
        if self.connected:
            return
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError as exc:  # pragma: no cover
            raise RelayConnectionError(
                "The 'websockets' package is required for the relay client"
            ) from exc

        jwt = sign_jwt(aud=self.relay_url, key_pair=self.auth_key)
        parsed = urlparse(self.relay_url)
        query = urlencode({"auth": jwt, "projectId": self.project_id, "ua": self.user_agent})
        url = urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", query, ""))
        headers: dict[str, str] = {"Authorization": f"Bearer {jwt}"}
        if self.origin:
            headers["Origin"] = self.origin

        try:
            self._ws = await ws_connect(
                url, additional_headers=headers, open_timeout=self.request_timeout
            )
        except Exception as exc:
            raise RelayConnectionError(f"Failed to connect to relay: {exc}") from exc

        self._connected = True
        self._closed.clear()
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="walletconnect-relay-reader"
        )

    async def close(self) -> None:
        """Close the WebSocket and cancel pending requests."""
        self._connected = False
        self._closed.set()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RelayConnectionError("Relay connection closed"))
        self._pending.clear()

    async def __aenter__(self) -> RelayClient:  # noqa: PYI034
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RelayConnectionError("Not connected to the relay")
        await self._ws.send(json.dumps(payload, separators=(",", ":")))

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC request and wait for the matching response."""
        if not self.connected:
            raise RelayConnectionError("Not connected to the relay")
        payload = make_request(method, params)
        request_id = int(payload["id"])
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = fut
        try:
            await self._send(payload)
            return await asyncio.wait_for(fut, timeout=self.request_timeout)
        except TimeoutError as exc:
            raise WalletConnectTimeoutError(
                f"Relay did not respond to {method} within {self.request_timeout}s"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def publish(
        self,
        topic: str,
        message: str,
        *,
        ttl: int,
        tag: int,
        attestation: str | None = None,
    ) -> bool:
        params: dict[str, Any] = {"topic": topic, "message": message, "ttl": ttl, "tag": tag}
        if attestation is not None:
            params["attestation"] = attestation
        result = await self.request("irn_publish", params)
        return bool(result)

    async def subscribe(self, topic: str) -> str:
        subscription_id = await self.request("irn_subscribe", {"topic": topic})
        if not isinstance(subscription_id, str):
            raise RelayError(f"Unexpected subscribe result: {subscription_id!r}")
        self._subscriptions[topic] = subscription_id
        return subscription_id

    async def unsubscribe(self, topic: str, subscription_id: str | None = None) -> bool:
        sub_id = subscription_id or self._subscriptions.get(topic)
        if sub_id is None:
            raise RelayError(f"No subscription id for topic {topic}")
        result = await self.request("irn_unsubscribe", {"topic": topic, "id": sub_id})
        self._subscriptions.pop(topic, None)
        return bool(result)

    async def fetch_messages(self, topic: str) -> tuple[list[RelayMessage], bool]:
        result = await self.request("irn_fetchMessages", {"topic": topic})
        if not isinstance(result, dict):
            raise RelayError(f"Unexpected fetchMessages result: {result!r}")
        messages = [
            RelayMessage(
                topic=item["topic"],
                message=item["message"],
                published_at=int(item["publishedAt"]),
                tag=int(item["tag"]),
                attestation=item.get("attestation"),
            )
            for item in result.get("messages", [])
        ]
        return messages, bool(result.get("hasMore", False))

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Ignoring non-JSON relay frame: %r", raw[:200])
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._connected:
                logger.warning("Relay reader stopped: %s", exc)
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(RelayConnectionError(str(exc)))
                self._pending.clear()
                self._connected = False
        finally:
            self._closed.set()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            method = message["method"]
            if method == "irn_subscription":
                await self._handle_subscription(message)
                return
            logger.debug("Ignoring unexpected relay request method %s", method)
            return

        request_id = message.get("id")
        if request_id is None:
            return
        fut = self._pending.get(int(request_id))
        if fut is None or fut.done():
            return
        if "error" in message:
            err = message["error"] or {}
            fut.set_exception(
                RelayRPCError(
                    int(err.get("code", -1)), str(err.get("message", "error")), err.get("data")
                )
            )
        else:
            fut.set_result(message.get("result"))

    async def _handle_subscription(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        data = params.get("data") or {}
        request_id = message.get("id")
        # Spec: client MUST ack with result: true.
        if request_id is not None:
            try:
                await self._send(make_result(request_id, True))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to ack irn_subscription: %s", exc)

        relay_message = RelayMessage(
            topic=str(data.get("topic", "")),
            message=str(data.get("message", "")),
            published_at=int(data.get("publishedAt") or 0),
            tag=int(data.get("tag") or 0),
            attestation=data.get("attestation"),
            subscription_id=params.get("id"),
        )
        # Run handlers as tasks so they can publish/request without deadlocking
        # the reader loop (publish waits for a JSON-RPC response on this socket).
        for handler in list(self._handlers):
            asyncio.create_task(
                self._run_handler(handler, relay_message),
                name="walletconnect-relay-handler",
            )

    async def _run_handler(self, handler: MessageHandler, relay_message: RelayMessage) -> None:
        try:
            result = handler(relay_message)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            logger.exception("Relay message handler failed")
