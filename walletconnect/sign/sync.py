"""Synchronous facade over :class:`SignClient` with a background event loop."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from walletconnect.constants import (
    DEFAULT_EIP155_EVENTS,
    DEFAULT_EIP155_METHODS,
    DEFAULT_RELAY_URL,
)

from .client import _CONNECT_TIMEOUT, _REQUEST_TIMEOUT, Session, SignClient

if TYPE_CHECKING:
    from walletconnect.crypto.jwt import AuthKeyPair
    from walletconnect.storage import KeyValueStorage
    from walletconnect.types import Metadata, ProposalNamespace


class SyncSignClient:
    """
    Thread-friendly wrapper for scripts and CLIs.

    Starts a background asyncio loop so relay keepalives and subscription acks
    continue while the main thread blocks on ``connect()`` / ``request()``.
    """

    def __init__(
        self,
        project_id: str | None = None,
        *,
        metadata: Metadata | dict[str, Any] | None = None,
        relay_url: str = DEFAULT_RELAY_URL,
        storage: KeyValueStorage | None = None,
        auth_key: AuthKeyPair | None = None,
        origin: str | None = None,
        connect_timeout: float = _CONNECT_TIMEOUT,
        request_timeout: float = _REQUEST_TIMEOUT,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="walletconnect-loop", daemon=True
        )
        self._thread.start()
        self._client = SignClient(
            project_id,
            metadata=metadata,
            relay_url=relay_url,
            storage=storage,
            auth_key=auth_key,
            origin=origin,
            connect_timeout=connect_timeout,
            request_timeout=request_timeout,
        )
        self._closed = False
        atexit.register(self.close)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Any) -> Any:
        if self._closed:
            raise RuntimeError("SyncSignClient is closed")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    @property
    def metadata(self) -> Metadata:
        """The dApp metadata sent to wallets in the session proposal."""
        return self._client.metadata

    @property
    def session(self) -> Session | None:
        return self._client.session

    @property
    def pairing_uri(self) -> str | None:
        return self._client.pairing_uri

    def connect(
        self,
        *,
        chains: Iterable[int] = (1,),
        methods: Iterable[str] = DEFAULT_EIP155_METHODS,
        events: Iterable[str] = DEFAULT_EIP155_EVENTS,
        optional_namespaces: dict[str, ProposalNamespace | dict[str, Any]] | None = None,
        on_uri: Any = None,
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
        self, method: str, params: Any, *, chain_id: str = "eip155:1", timeout: float | None = None
    ) -> Any:
        return self._submit(
            self._client.request(method, params, chain_id=chain_id, timeout=timeout)
        )

    def personal_sign(
        self, message: str | bytes, address: str, *, chain_id: str = "eip155:1"
    ) -> str:
        return self._submit(self._client.personal_sign(message, address, chain_id=chain_id))

    def ping(self) -> bool:
        return self._submit(self._client.ping())

    def disconnect(self, reason: str = "User disconnected") -> None:
        self._submit(self._client.disconnect(reason))

    def close(self) -> None:
        if self._closed:
            return
        with contextlib.suppress(Exception):
            fut = asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
            fut.result(timeout=10)
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def __enter__(self) -> SyncSignClient:  # noqa: PYI034
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
