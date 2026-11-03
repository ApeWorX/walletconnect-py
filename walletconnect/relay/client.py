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
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode, urlparse, urlunparse

from websockets.exceptions import ConnectionClosed

from walletconnect.constants import (
    DEFAULT_RELAY_URL,
    PROJECT_ID_ENV_VAR,
    USER_AGENT,
)
from walletconnect.crypto.jwt import AuthKeyPair, generate_auth_key_pair, sign_jwt
from walletconnect.exceptions import (
    MissingProjectIdError,
    RelayConnectionError,
    RelayDisconnectedError,
    RelayError,
    WalletConnectTimeoutError,
)
from walletconnect.utils.rpc_id import coerce_rpc_id

from .rpc import RelayRPCError, make_request, make_result

logger = logging.getLogger(__name__)

MessageHandler = Callable[["RelayMessage"], Awaitable[None] | None]
ConnectionEvent = Literal["disconnect", "reconnect"]
ConnectionHandler = Callable[[ConnectionEvent], Awaitable[None] | None]

_DEFAULT_REQUEST_TIMEOUT = 30.0
# Close codes the relay uses for "going away / rebalancing": reconnect at once, no backoff.
_RECONNECT_CLOSE_CODES = {1001, 4010}
# HTTP statuses on the WebSocket upgrade that retrying will not fix (bad project ID / auth).
_FATAL_HTTP_STATUSES = {400, 401, 403}
_MAX_FETCH_PAGES = 20
# A connection that dies sooner than this after opening does not reset the backoff,
# so a relay that accepts then immediately drops us is not hammered.
_STABLE_CONNECTION_SECONDS = 10.0


class _FatalConnectError(RelayConnectionError):
    """The relay refused the handshake in a way retrying will not fix."""


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

    **Auto-reconnect** (on by default): if the WebSocket drops for any reason other than
    :meth:`close`, the client reconnects with exponential backoff (``reconnect_min_delay``
    doubling up to ``reconnect_max_delay``, with jitter), signs a fresh auth JWT, re-subscribes
    every topic and runs ``irn_fetchMessages`` on each so messages published while offline are
    still delivered to :meth:`on_message` handlers (they may see a message twice; dedupe
    upstream). Relay requests made while disconnected, or in flight when the socket dropped,
    are re-sent once the connection is back (publish/subscribe are idempotent on the relay);
    if it is not back within ``request_timeout`` they raise
    :class:`~walletconnect.exceptions.RelayDisconnectedError`.
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
        auto_reconnect: bool = True,
        reconnect_min_delay: float = 0.5,
        reconnect_max_delay: float = 30.0,
        reconnect_max_attempts: int | None = None,
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
        self.auto_reconnect = auto_reconnect
        self.reconnect_min_delay = reconnect_min_delay
        self.reconnect_max_delay = reconnect_max_delay
        self.reconnect_max_attempts = reconnect_max_attempts

        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: dict[str, str] = {}  # topic -> subscription id
        self._handlers: list[MessageHandler] = []
        self._connection_handlers: list[ConnectionHandler] = []
        self._closed = asyncio.Event()
        self._ready = asyncio.Event()  # set while connected (or when giving up / closing)
        self._connected = False
        self._closing = False
        self._fatal: BaseException | None = None
        self.reconnect_count = 0
        self._connected_at = 0.0
        self._backoff_attempt = 0

    @property
    def client_id(self) -> str:
        """The persistent ``did:key:…`` presented to the relay."""
        return self.auth_key.did_key

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    @property
    def reconnecting(self) -> bool:
        """``True`` while an automatic reconnect is in progress."""
        return self._reconnect_task is not None and not self._reconnect_task.done()

    @property
    def subscriptions(self) -> dict[str, str]:
        """Active subscriptions as ``{topic: subscription_id}`` (a copy)."""
        return dict(self._subscriptions)

    def on_message(self, handler: MessageHandler) -> None:
        """Register a callback invoked for every ``irn_subscription`` message."""
        self._handlers.append(handler)

    def on_connection_event(self, handler: ConnectionHandler) -> None:
        """
        Register a callback for ``"disconnect"`` (socket dropped unexpectedly) and
        ``"reconnect"`` (connection restored and topics re-subscribed).
        """
        self._connection_handlers.append(handler)

    async def connect(self) -> None:
        """Open the WebSocket and start the reader loop."""
        if self.connected:
            return
        if self.reconnecting:
            # An automatic reconnect is already running; wait for it instead of racing it.
            await self._wait_ready(self.request_timeout)
            if not self.connected:
                raise RelayDisconnectedError("Relay reconnect did not complete")
            return
        self._closing = False
        self._fatal = None
        await self._open_socket()

    async def _open_socket(self) -> None:
        try:
            from websockets.asyncio.client import connect as ws_connect
            from websockets.exceptions import InvalidStatus
        except ImportError as exc:  # pragma: no cover
            raise RelayConnectionError(
                "The 'websockets' package is required for the relay client"
            ) from exc

        # A fresh JWT on every (re)connect: the previous one may have expired.
        jwt = sign_jwt(aud=self.relay_url, key_pair=self.auth_key)
        parsed = urlparse(self.relay_url)
        query = urlencode({"auth": jwt, "projectId": self.project_id, "ua": self.user_agent})
        url = urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", query, ""))
        headers: dict[str, str] = {"Authorization": f"Bearer {jwt}"}
        if self.origin:
            headers["Origin"] = self.origin

        try:
            ws = await ws_connect(
                url, additional_headers=headers, open_timeout=self.request_timeout
            )
        except InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in _FATAL_HTTP_STATUSES:
                raise _FatalConnectError(f"Relay rejected the connection: {exc}") from exc
            raise RelayConnectionError(f"Failed to connect to relay: {exc}") from exc
        except Exception as exc:
            raise RelayConnectionError(f"Failed to connect to relay: {exc}") from exc

        self._ws = ws
        self._connected = True
        self._connected_at = asyncio.get_running_loop().time()
        self._closed.clear()
        self._ready.set()
        self._reader_task = asyncio.create_task(
            self._read_loop(ws), name="walletconnect-relay-reader"
        )

    async def close(self) -> None:
        """Close the WebSocket, stop reconnecting, and cancel pending requests."""
        self._closing = True
        self._connected = False
        self._closed.set()
        self._ready.set()  # wake waiters; they observe _closing and fail
        current = asyncio.current_task()
        for task in (self._reconnect_task, self._reader_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reconnect_task = None
        self._reader_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        self._fail_pending(RelayDisconnectedError("Relay connection closed"))

    async def __aenter__(self) -> RelayClient:  # noqa: PYI034
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------- reconnect

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    def _on_connection_lost(self, ws: Any, reason: str) -> None:
        if self._closing or ws is not self._ws:
            return
        self._connected = False
        self._ws = None
        self._ready.clear()
        code = getattr(ws, "close_code", None)
        # In-flight requests get a retryable error; request() re-sends after reconnect.
        self._fail_pending(RelayDisconnectedError(f"Relay connection lost: {reason}"))
        self._emit_connection_event("disconnect")
        if not self.auto_reconnect:
            logger.warning("Relay connection lost (%s); auto-reconnect disabled", reason)
            self._ready.set()
            return
        lived = asyncio.get_running_loop().time() - self._connected_at
        if lived >= _STABLE_CONNECTION_SECONDS:
            self._backoff_attempt = 0
        logger.warning("Relay connection lost (code=%s, %s); reconnecting", code, reason)
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(
                immediate=code in _RECONNECT_CLOSE_CODES and self._backoff_attempt == 0
            ),
            name="walletconnect-relay-reconnect",
        )

    def _backoff(self, attempt: int) -> float:
        delay = min(self.reconnect_max_delay, self.reconnect_min_delay * (2 ** min(attempt, 30)))
        # "Equal jitter": somewhere in [delay/2, delay] so clients don't stampede.
        return float(delay / 2 + random.uniform(0, delay / 2))  # noqa: S311

    async def _reconnect_loop(self, *, immediate: bool) -> None:
        attempt = 0
        while not self._closing:
            if not (immediate and attempt == 0):
                await asyncio.sleep(self._backoff(self._backoff_attempt))
            attempt += 1
            self._backoff_attempt += 1
            try:
                await self._open_socket()
            except _FatalConnectError as exc:
                logger.error("Giving up reconnecting: %s", exc)
                self._fatal = exc
                self._ready.set()
                return
            except RelayConnectionError as exc:
                if (
                    self.reconnect_max_attempts is not None
                    and attempt >= self.reconnect_max_attempts
                ):
                    logger.error("Giving up reconnecting after %d attempts: %s", attempt, exc)
                    self._fatal = exc
                    self._ready.set()
                    return
                logger.info("Relay reconnect attempt %d failed: %s", attempt, exc)
                continue
            try:
                await self._resubscribe()
            except RelayConnectionError as exc:
                # Dropped again mid-resubscribe; the reader's loss handler starts a new loop.
                logger.info("Relay dropped during resubscribe: %s", exc)
                return
            except Exception:  # noqa: BLE001
                logger.exception("Relay resubscribe failed")
            self.reconnect_count += 1
            logger.info("Relay reconnected after %d attempt(s)", attempt)
            self._emit_connection_event("reconnect")
            return

    async def _resubscribe(self) -> None:
        topics = list(self._subscriptions)
        # retry=False: if we drop again, the new reconnect loop owns the retry.
        for topic in topics:
            sub_id = await self.request("irn_subscribe", {"topic": topic}, retry=False)
            if isinstance(sub_id, str) and topic in self._subscriptions:
                self._subscriptions[topic] = sub_id
        # Anything published while we were offline (the relay mailbox).
        for topic in topics:
            for _ in range(_MAX_FETCH_PAGES):
                messages, has_more = await self.fetch_messages(topic, retry=False)
                for message in messages:
                    self._deliver_to_handlers(message)
                if not has_more:
                    break

    def _emit_connection_event(self, event: ConnectionEvent) -> None:
        for handler in list(self._connection_handlers):
            asyncio.create_task(
                self._run_connection_handler(handler, event),
                name=f"walletconnect-relay-{event}",
            )

    async def _run_connection_handler(self, handler: ConnectionHandler, event: str) -> None:
        try:
            result = handler(event)  # type: ignore[arg-type]
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            logger.exception("Relay connection handler failed")

    async def _wait_ready(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=max(0.0, timeout))

    # --------------------------------------------------------------- requests

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RelayDisconnectedError("Not connected to the relay")
        await self._ws.send(json.dumps(payload, separators=(",", ":")))

    def _unavailable(self) -> RelayConnectionError | None:
        """The error to raise right now if we cannot wait for a reconnect, else ``None``."""
        if self._closing:
            return RelayDisconnectedError("Relay client is closed")
        if self._fatal is not None:
            return RelayDisconnectedError(f"Relay reconnect gave up: {self._fatal}")
        if not self.reconnecting:
            return RelayDisconnectedError("Not connected to the relay")
        return None

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, retry: bool = True
    ) -> Any:
        """
        Send a JSON-RPC request and wait for the matching response.

        If the connection is down (or drops before the response arrives) and auto-reconnect
        is running, waits for it and re-sends, all within ``request_timeout``. Pass
        ``retry=False`` to fail fast with :class:`RelayDisconnectedError` instead.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.request_timeout
        while True:
            if not self.connected:
                if not retry:
                    raise RelayDisconnectedError("Not connected to the relay")
                if (err := self._unavailable()) is not None:
                    raise err
                await self._wait_ready(deadline - loop.time())
                if not self.connected:
                    if (err := self._unavailable()) is not None:
                        raise err
                    raise RelayDisconnectedError(
                        f"Relay connection not restored within {self.request_timeout}s "
                        f"({method} not sent)"
                    )
            payload = make_request(method, params)
            request_id = coerce_rpc_id(payload["id"])
            fut: asyncio.Future[Any] = loop.create_future()
            self._pending[request_id] = fut
            try:
                await self._send(payload)
                return await asyncio.wait_for(fut, timeout=max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError as exc:
                raise WalletConnectTimeoutError(
                    f"Relay did not respond to {method} within {self.request_timeout}s"
                ) from exc
            except (RelayDisconnectedError, ConnectionClosed) as exc:
                # Retry after reconnect (the loss handler has already run, or will shortly).
                if not retry or self._closing or not self.auto_reconnect or loop.time() >= deadline:
                    raise RelayDisconnectedError(f"Relay connection lost during {method}") from exc
                logger.info("Retrying %s after relay reconnect", method)
                await asyncio.sleep(0.01)  # let the reader notice the drop
                continue
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

    async def fetch_messages(
        self, topic: str, *, retry: bool = True
    ) -> tuple[list[RelayMessage], bool]:
        result = await self.request("irn_fetchMessages", {"topic": topic}, retry=retry)
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

    async def _read_loop(self, ws: Any) -> None:
        reason = "connection closed"
        try:
            async for raw in ws:
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
            reason = str(exc) or type(exc).__name__
        finally:
            if ws is self._ws:
                self._closed.set()
        self._on_connection_lost(ws, reason)

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
        try:
            pending_key = coerce_rpc_id(request_id)
        except ValueError:
            return
        fut = self._pending.get(pending_key)
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
        self._deliver_to_handlers(relay_message)

    def _deliver_to_handlers(self, relay_message: RelayMessage) -> None:
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
