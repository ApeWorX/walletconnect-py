"""
Client events for long-running sessions.

Register handlers with :meth:`SignClient.on() <walletconnect.sign.SignClient.on>`. Each handler
gets one :class:`ClientEvent`. Handlers may be plain functions or coroutines; they run as tasks
on the client's event loop, and exceptions are logged, never raised into the client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

if TYPE_CHECKING:
    from .state import Session

EventName = Literal[
    "session_update",
    "session_event",
    "session_delete",
    "session_expire",
    "session_extend",
    "session_expiring",
    "relay_disconnect",
    "relay_reconnect",
]
"""
Names accepted by :meth:`SignClient.on`:

``session_update``
    The wallet (session controller) replaced the session namespaces, e.g. added an account or
    chain. ``session`` holds the new namespaces; ``data["namespaces"]`` too.
``session_event``
    The wallet emitted ``accountsChanged`` / ``chainChanged`` (``data["name"]``,
    ``data["data"]``, ``data["chain_id"]``). A UI hint only: the authoritative account list is
    still ``session.namespaces``, which changes only through ``session_update``.
``session_delete``
    The wallet ended the session (``data["code"]``, ``data["message"]``). Pending requests
    fail with :class:`~walletconnect.exceptions.SessionDeletedError`.
``session_expire``
    The session's expiry passed (a local timer; the wallet sends nothing). Pending requests fail
    with :class:`~walletconnect.exceptions.SessionExpiredError`.
``session_extend``
    The expiry moved forward (``data["expiry"]``; ``data["by"]`` is ``"wallet"`` or ``"dapp"``).
``session_expiring``
    Less than ``expiring_warning`` seconds (default 24h) remain and no extension is confirmed
    (``data["expiry"]``, ``data["remaining"]``). Emitted at most once per maintenance cycle.
``relay_disconnect`` / ``relay_reconnect``
    The relay WebSocket dropped / came back (topics re-subscribed, missed messages fetched).
    ``topic`` and ``session`` are ``None``.
"""

EVENT_NAMES: frozenset[str] = frozenset(get_args(EventName))


@dataclass(frozen=True)
class ClientEvent:
    """One event delivered to handlers registered with :meth:`SignClient.on`."""

    name: EventName
    topic: str | None = None
    """Session topic (``None`` for relay events)."""
    session: Session | None = None
    """A snapshot of the session after the change (before removal for delete/expire)."""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form **without secrets** (no session key), e.g. for SSE or logs."""
        session = None
        if self.session is not None:
            session = {
                "topic": self.session.topic,
                "expiry": self.session.expiry,
                "peer_name": self.session.peer_name,
                "accounts": self.session.accounts,
                "namespaces": self.session.namespaces,
            }
        return {"name": self.name, "topic": self.topic, "session": session, "data": self.data}


EventHandler = Callable[[ClientEvent], Awaitable[None] | None]

__all__ = ["EVENT_NAMES", "ClientEvent", "EventHandler", "EventName"]
