"""Synchronous facade over :class:`SignClient` with a background event loop."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import threading
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from walletconnect.constants import (
    DEFAULT_EIP155_EVENTS,
    DEFAULT_EIP155_METHODS,
    DEFAULT_RELAY_URL,
)

from .client import (
    _AUTO_EXTEND_THRESHOLD,
    _CONNECT_TIMEOUT,
    _EXPIRING_WARNING,
    _MAINTENANCE_INTERVAL,
    _PING_TIMEOUT,
    _REQUEST_TIMEOUT,
    SignClient,
)

if TYPE_CHECKING:
    from walletconnect.types import Metadata, ProposalNamespace

    from .events import EventHandler
    from .state import ClientState, Session


class SyncSignClient:
    """
    Thread-friendly wrapper for scripts and CLIs.

    Starts a background asyncio loop so relay keepalives and subscription acks
    continue while the main thread blocks on ``connect()`` / ``request()``.

    Takes the same ``state=`` / ``on_state_change=`` arguments as :class:`SignClient`
    (``on_state_change`` runs on the background loop thread).

    Event handlers registered with :meth:`on` also run on the background loop thread, so keep
    them quick and thread-safe (e.g. put the event on a :class:`queue.Queue`).
    """

    def __init__(
        self,
        project_id: str | None = None,
        *,
        metadata: Metadata | dict[str, Any] | None = None,
        relay_url: str = DEFAULT_RELAY_URL,
        state: ClientState | None = None,
        on_state_change: Callable[[ClientState], Any] | None = None,
        origin: str | None = None,
        connect_timeout: float = _CONNECT_TIMEOUT,
        request_timeout: float = _REQUEST_TIMEOUT,
        auto_reconnect: bool = True,
        auto_extend: bool = False,
        auto_extend_threshold: float = _AUTO_EXTEND_THRESHOLD,
        expiring_warning: float = _EXPIRING_WARNING,
        maintenance_interval: float = _MAINTENANCE_INTERVAL,
    ) -> None:
        self._client = SignClient(
            project_id,
            metadata=metadata,
            relay_url=relay_url,
            state=state,
            on_state_change=on_state_change,
            origin=origin,
            connect_timeout=connect_timeout,
            request_timeout=request_timeout,
            auto_reconnect=auto_reconnect,
            auto_extend=auto_extend,
            auto_extend_threshold=auto_extend_threshold,
            expiring_warning=expiring_warning,
            maintenance_interval=maintenance_interval,
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="walletconnect-loop", daemon=True
        )
        self._thread.start()
        self._closed = False
        atexit.register(self.close)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Any) -> Any:
        if self._closed:
            coro.close()
            raise RuntimeError("SyncSignClient is closed")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    @property
    def metadata(self) -> Metadata:
        """The dApp metadata sent to wallets in the session proposal."""
        return self._client.metadata

    @property
    def client_id(self) -> str:
        return self._client.client_id

    @property
    def session(self) -> Session | None:
        return self._client.session

    @property
    def sessions(self) -> dict[str, Session]:
        return self._client.sessions

    @property
    def pairing_uri(self) -> str | None:
        return self._client.pairing_uri

    def export_state(self) -> ClientState:
        """See :meth:`SignClient.export_state`. Contains secrets; store it securely."""
        return self._client.export_state()

    def open(self) -> None:
        """Connect and resume restored sessions now (otherwise done on first use)."""
        self._submit(self._client.open())

    def connect(
        self,
        *,
        chains: Iterable[int] = (1,),
        methods: Iterable[str] = DEFAULT_EIP155_METHODS,
        events: Iterable[str] = DEFAULT_EIP155_EVENTS,
        optional_namespaces: dict[str, ProposalNamespace | dict[str, Any]] | None = None,
        on_uri: Callable[[str], Any] | None = None,
    ) -> Session:
        """
        Propose a session, invoke ``on_uri(uri)`` (e.g. print QR), then block until settled.
        """
        uri, task = self._submit(
            self._client.connect(
                chains=chains,
                methods=methods,
                events=events,
                optional_namespaces=optional_namespaces,
            )
        )
        if on_uri is not None:
            on_uri(uri)
        return self._submit(self._client.wait_for_session(task))

    def request(
        self,
        method: str,
        params: Any,
        *,
        chain_id: str = "eip155:1",
        topic: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        return self._submit(
            self._client.request(method, params, chain_id=chain_id, topic=topic, timeout=timeout)
        )

    def personal_sign(
        self,
        message: str | bytes,
        address: str,
        *,
        chain_id: str = "eip155:1",
        topic: str | None = None,
    ) -> str:
        return self._submit(
            self._client.personal_sign(message, address, chain_id=chain_id, topic=topic)
        )

    @property
    def unreachable(self) -> frozenset[str]:
        return self._client.unreachable

    def on(self, name: str, handler: EventHandler | None = None) -> Any:
        """See :meth:`SignClient.on`. Handlers run on the background loop thread."""
        return self._client.on(name, handler)

    def off(self, name: str, handler: EventHandler) -> None:
        self._client.off(name, handler)

    def ping(self, *, topic: str | None = None, timeout: float = _PING_TIMEOUT) -> bool:
        return self._submit(self._client.ping(topic=topic, timeout=timeout))

    def is_reachable(self, *, topic: str | None = None, timeout: float = 10.0) -> bool:
        return self._submit(self._client.is_reachable(topic=topic, timeout=timeout))

    def extend(self, *, topic: str | None = None, timeout: float | None = None) -> int:
        return self._submit(self._client.extend(topic=topic, timeout=timeout))

    def disconnect(self, reason: str = "User disconnected", *, topic: str | None = None) -> None:
        self._submit(self._client.disconnect(reason, topic=topic))

    def close(self) -> None:
        if self._closed:
            return
        with contextlib.suppress(Exception):
            fut = asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
            fut.result(timeout=10)
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        atexit.unregister(self.close)

    def __enter__(self) -> SyncSignClient:  # noqa: PYI034
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
