"""
Async Sign API client (dApp = proposer).

Flow: connect → print pairing URI/QR → wallet settles session → request signatures.

The client is **stateless**: it never reads or writes files. Its relay identity key and
settled sessions live in memory as a :class:`~walletconnect.sign.state.ClientState`, which you
can export with :meth:`SignClient.export_state` and pass back in with ``SignClient(state=...)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from walletconnect.constants import (
    DEFAULT_EIP155_EVENTS,
    DEFAULT_EIP155_METHODS,
    DEFAULT_METADATA_NAME,
    DEFAULT_METADATA_URL,
    DEFAULT_RELAY_URL,
    EIP155_NAMESPACE,
    PAIRING_PROPOSAL_EXPIRY,
    RELAY_PROTOCOL,
    SESSION_EXPIRY,
    SIGN_RPC_OPTS,
)
from walletconnect.crypto.codec import decode_jsonrpc, encode_jsonrpc
from walletconnect.crypto.envelope import TYPE_0
from walletconnect.crypto.keys import (
    KeyPair,
    derive_sym_key,
    generate_key_pair,
    topic_from_sym_key,
)
from walletconnect.exceptions import (
    InvalidUpdateRequestError,
    NoActiveSessionError,
    SdkError,
    SessionDeletedError,
    SessionExpiredError,
    UnauthorizedUpdateRequestError,
    WalletConnectError,
    WalletConnectTimeoutError,
)
from walletconnect.pairing.uri import PairingURI
from walletconnect.relay.client import RelayClient, RelayMessage
from walletconnect.types import Metadata, ProposalNamespace
from walletconnect.utils.ids import payload_id
from walletconnect.utils.rpc_id import coerce_rpc_id

from .events import EVENT_NAMES, ClientEvent, EventHandler
from .state import ClientState, Session

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 300.0  # 5 minutes for the user to scan + approve
_REQUEST_TIMEOUT = 300.0
_SEEN_CACHE_SIZE = 2048
_MAX_FETCH_PAGES = 20
_DAY = 24 * 60 * 60
_AUTO_EXTEND_THRESHOLD = 2 * _DAY
_EXPIRING_WARNING = _DAY
_MAINTENANCE_INTERVAL = 60 * 60
_PING_TIMEOUT = 30.0

StateChangeCallback = Callable[[ClientState], Awaitable[None] | None]


@dataclass
class _Proposal:
    """An in-flight ``wc_sessionPropose`` on one pairing topic (in memory only)."""

    uri: PairingURI
    self_key: KeyPair
    request_id: int


def _resolve_metadata(metadata: Metadata | dict[str, Any] | None) -> Metadata:
    """Return the outbound dApp metadata, validating what wallets will see."""
    if metadata is None:
        logger.info(
            "No dApp metadata passed; wallets will show the neutral default "
            "(%r, %s). Pass metadata=Metadata(name=..., url=...) to identify your app.",
            DEFAULT_METADATA_NAME,
            DEFAULT_METADATA_URL,
        )
        return Metadata.default()

    resolved = metadata if isinstance(metadata, Metadata) else Metadata.model_validate(metadata)
    if not resolved.name.strip():
        raise ValueError("metadata name must not be empty")
    if not resolved.url.strip():
        logger.warning(
            "dApp metadata url is empty; some wallets (e.g. MetaMask Mobile) reject session "
            "proposals without a valid url. Set metadata url to your app's https URL."
        )
    elif not resolved.url.startswith(("https://", "http://")):
        raise ValueError("metadata url must be an http(s) URL, e.g. 'https://example.com'")
    return resolved


class SignClient:
    """
    Async dApp-side WalletConnect Sign client.

    ``metadata`` identifies your app to the wallet (sent as ``proposer.metadata`` in
    ``wc_sessionPropose``). Accepts a :class:`~walletconnect.types.Metadata` or an equivalent
    dict; defaults to :meth:`Metadata.default`.

    ``state`` resumes a previous client: its relay identity key and settled sessions (see
    :class:`~walletconnect.sign.state.ClientState`). Without it, the client starts with a fresh
    relay key and no sessions. The client copies ``state``; read the current state back with
    :meth:`export_state`. Nothing is ever written to disk by the library.

    ``on_state_change`` (optional) is called with a fresh :class:`ClientState` snapshot whenever
    the persistable state changes (session settled, updated, extended, deleted or expired), so
    you can save it.

    ``auto_reconnect`` (default ``True``): if the relay WebSocket drops, reconnect with capped,
    jittered backoff, re-subscribe every session/pairing topic and fetch anything published
    meanwhile. Wallet requests in flight keep waiting (their responses are queued by the relay);
    relay calls made while down are re-sent once reconnected, or raise
    :class:`~walletconnect.exceptions.RelayDisconnectedError` if the relay is not back within
    the relay request timeout.

    Long-running sessions (see the "Long-running sessions" guide):

    - Register event handlers with :meth:`on` (``session_update``, ``session_event``,
      ``session_delete``, ``session_expire``, ``session_extend``, ``session_expiring``,
      ``relay_disconnect``, ``relay_reconnect``).
    - Expiry is checked on load, before every request, and by a local timer; requests waiting
      on a session that expires or is deleted fail right away.
    - :meth:`extend` asks the wallet for 7 more days. With ``auto_extend=True`` the client does
      it on its own once less than ``auto_extend_threshold`` seconds (default 2 days) remain,
      retrying every ``maintenance_interval`` (default 1h) until the wallet confirms; it logs a
      warning and emits ``session_expiring`` once less than ``expiring_warning`` (default 24h)
      remains unconfirmed.
    - :meth:`is_reachable` pings the wallet and marks the session unreachable (not deleted)
      when it does not answer.
    """

    def __init__(
        self,
        project_id: str | None = None,
        *,
        metadata: Metadata | dict[str, Any] | None = None,
        relay_url: str = DEFAULT_RELAY_URL,
        state: ClientState | None = None,
        on_state_change: StateChangeCallback | None = None,
        origin: str | None = None,
        connect_timeout: float = _CONNECT_TIMEOUT,
        request_timeout: float = _REQUEST_TIMEOUT,
        auto_reconnect: bool = True,
        auto_extend: bool = False,
        auto_extend_threshold: float = _AUTO_EXTEND_THRESHOLD,
        expiring_warning: float = _EXPIRING_WARNING,
        maintenance_interval: float = _MAINTENANCE_INTERVAL,
    ) -> None:
        self.metadata = _resolve_metadata(metadata)
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.on_state_change = on_state_change
        self.auto_extend = auto_extend
        self.auto_extend_threshold = auto_extend_threshold
        self.expiring_warning = expiring_warning
        self.maintenance_interval = maintenance_interval

        self._state = state.model_copy(deep=True) if state is not None else ClientState()
        # Expiry is checked on load; the session_expire events go out on the first open().
        self._expired_on_load = [s for s in self._state.sessions.values() if s.is_expired()]
        if self._state.prune_expired():
            logger.info(
                "Dropped %d expired session(s) from restored state", len(self._expired_on_load)
            )
        self.auth_key = self._state.auth_key

        self.relay = RelayClient(
            project_id,
            relay_url=relay_url,
            auth_key=self.auth_key,
            origin=origin,
            request_timeout=max(30.0, min(request_timeout, 60.0)),
            auto_reconnect=auto_reconnect,
        )
        self.relay.on_message(self._on_relay_message)
        self.relay.on_connection_event(self._on_relay_connection_event)

        # topic -> symKey for every topic we can decrypt (sessions + in-flight pairings).
        self._sym_keys: dict[str, str] = {t: s.sym_key for t, s in self._state.sessions.items()}
        self._proposals: dict[str, _Proposal] = {}  # pairing topic -> proposal
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._settle_buffer: dict[str, dict[str, Any]] = {}  # session topic -> settle request
        self._settle_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pairing_uri: PairingURI | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._open_lock = asyncio.Lock()
        self._opened = False
        # Long-running session bookkeeping (in memory only).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handlers: dict[str, list[EventHandler]] = {}
        self._pending_topics: dict[int, str] = {}  # request id -> session topic
        self._expiry_timers: dict[str, asyncio.TimerHandle] = {}
        self._extending: dict[str, asyncio.Future[int]] = {}
        self._unreachable: set[str] = set()
        self._maintenance_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ state

    def export_state(self) -> ClientState:
        """
        Return a snapshot of the persistable state (relay key + settled sessions).

        Serialize it with ``state.model_dump_json()`` (or ``state.save(path)``) and pass it
        back as ``SignClient(state=ClientState.model_validate_json(...))``.

        .. warning:: The snapshot contains secret keys. Store it securely.
        """
        self._prune_expired(notify=False)
        return self._state.model_copy(deep=True)

    @property
    def client_id(self) -> str:
        """The relay ``did:key`` identity (stable across restarts when state is restored)."""
        return self.auth_key.did_key

    @property
    def sessions(self) -> dict[str, Session]:
        """Active (non-expired) sessions keyed by topic."""
        self._prune_expired(notify=False)
        return dict(self._state.sessions)

    @property
    def session(self) -> Session | None:
        """
        The only active session, or ``None`` when there is none.

        Raises :class:`~walletconnect.exceptions.NoActiveSessionError` when several sessions
        exist; use :attr:`sessions` and pass ``topic=`` to requests instead.
        """
        sessions = self.sessions
        if not sessions:
            return None
        if len(sessions) > 1:
            raise NoActiveSessionError(
                f"{len(sessions)} sessions are active; use .sessions and pass topic="
            )
        return next(iter(sessions.values()))

    @property
    def pairing_uri(self) -> str | None:
        """The most recent pairing URI returned by :meth:`connect`."""
        return str(self._pairing_uri) if self._pairing_uri else None

    @property
    def pending_pairings(self) -> list[str]:
        """Pairing topics with a session proposal still awaiting the wallet."""
        return list(self._proposals)

    def _prune_expired(self, *, notify: bool = True) -> list[str]:
        gone = [s for s in self._state.sessions.values() if s.is_expired()]
        for session in gone:
            self._remove_session(session.topic)
            logger.info("Session %s… expired", session.topic[:16])
            self._call_on_loop(self._after_expire, session)
        if gone and notify:
            self._schedule_state_change()
        return [s.topic for s in gone]

    def _expire_now(self, topic: str) -> None:
        session = self._remove_session(topic)
        if session is None:
            return
        logger.info("Session %s… expired", topic[:16])
        self._after_expire(session)
        self._schedule_state_change()

    def _after_expire(self, session: Session) -> None:
        self._after_session_gone(
            session,
            "session_expire",
            {"expiry": session.expiry},
            lambda: SessionExpiredError(f"Session {session.topic[:16]}… has expired"),
        )

    def _after_session_gone(
        self,
        session: Session,
        event: str,
        data: dict[str, Any],
        error: Callable[[], BaseException],
    ) -> None:
        """Fail requests waiting on a removed session, emit ``event`` and unsubscribe."""
        self._fail_pending_for(session.topic, error)
        self._emit(event, session.topic, session=session, data=data)
        if self.relay.connected and session.topic in self.relay.subscriptions:
            asyncio.get_running_loop().create_task(self._unsubscribe_quietly(session.topic))

    async def _unsubscribe_quietly(self, topic: str) -> None:
        with contextlib.suppress(Exception):
            await self.relay.unsubscribe(topic)

    def _call_on_loop(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run ``fn`` on the client loop (callers may be on another thread, e.g. sync use)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fn(*args)
        else:
            loop.call_soon_threadsafe(fn, *args)

    def _resolve_session(self, topic: str | None) -> Session:
        if topic is not None:
            session = self._state.sessions.get(topic)
            if session is None:
                raise NoActiveSessionError(f"No session for topic {topic[:16]}…")
            if session.is_expired():
                self._remove_session(topic)
                self._call_on_loop(self._after_expire, session)
                self._schedule_state_change()
                raise SessionExpiredError(f"Session {topic[:16]}… has expired")
            return session

        self._prune_expired()
        sessions = self._state.sessions
        if not sessions:
            raise NoActiveSessionError("No active session; call connect() first")
        if len(sessions) > 1:
            raise NoActiveSessionError(
                f"{len(sessions)} sessions are active; pass topic= to choose one"
            )
        return next(iter(sessions.values()))

    def _remove_session(self, topic: str) -> Session | None:
        session = self._state.sessions.pop(topic, None)
        self._sym_keys.pop(topic, None)
        self._unreachable.discard(topic)
        timer = self._expiry_timers.pop(topic, None)
        if timer is not None:
            timer.cancel()
        return session

    # ----------------------------------------------------------------- events

    def on(self, name: str, handler: EventHandler | None = None) -> Any:
        """
        Register ``handler`` for event ``name`` (or ``"*"`` for all events).

        Handlers get a :class:`~walletconnect.sign.events.ClientEvent`; they may be sync or async
        and run as tasks on the client's loop. Returns the handler. Also usable as a decorator::

            @client.on("session_delete")
            def gone(event): ...
        """
        if name != "*" and name not in EVENT_NAMES:
            raise ValueError(f"Unknown event {name!r}; expected one of {sorted(EVENT_NAMES)}")

        def register(fn: EventHandler) -> EventHandler:
            self._handlers.setdefault(name, []).append(fn)
            return fn

        return register(handler) if handler is not None else register

    def off(self, name: str, handler: EventHandler) -> None:
        """Remove a handler registered with :meth:`on` (no-op if it isn't registered)."""
        with contextlib.suppress(ValueError):
            self._handlers.get(name, []).remove(handler)

    def _emit(
        self,
        name: str,
        topic: str | None = None,
        *,
        session: Session | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        handlers = [*self._handlers.get(name, ()), *self._handlers.get("*", ())]
        if not handlers:
            return
        if session is None and topic is not None:
            session = self._state.sessions.get(topic)
        event = ClientEvent(
            name=name,  # type: ignore[arg-type]
            topic=topic,
            session=session.model_copy(deep=True) if session is not None else None,
            data=dict(data or {}),
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for handler in handlers:
            loop.create_task(self._run_handler(handler, event), name=f"walletconnect-{name}")

    @staticmethod
    async def _run_handler(handler: EventHandler, event: ClientEvent) -> None:
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            logger.exception("%s handler failed", event.name)

    def _on_relay_connection_event(self, event: str) -> None:
        if event == "disconnect":
            self._emit("relay_disconnect")
        elif event == "reconnect":
            self._emit("relay_reconnect")
            self._prune_expired()

    # ------------------------------------------------------- pending requests

    def _track(self, request_id: int, topic: str, fut: asyncio.Future[Any]) -> None:
        self._pending[request_id] = fut
        self._pending_topics[request_id] = topic

    def _untrack(self, request_id: int) -> None:
        self._pending.pop(request_id, None)
        self._pending_topics.pop(request_id, None)

    def _fail_pending_for(self, topic: str, error: Callable[[], BaseException]) -> None:
        for request_id, pending_topic in list(self._pending_topics.items()):
            fut = self._pending.get(request_id)
            if pending_topic == topic and fut is not None and not fut.done():
                fut.set_exception(error())

    # ----------------------------------------------------------- expiry timer

    def _arm_expiry(self, session: Session) -> None:
        """(Re)start the local timer that drops ``session`` at its expiry."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        old = self._expiry_timers.pop(session.topic, None)
        if old is not None:
            old.cancel()
        delay = max(0.0, session.expiry - time.time())
        self._expiry_timers[session.topic] = loop.call_later(
            delay, self._on_expiry_timer, session.topic
        )

    def _on_expiry_timer(self, topic: str) -> None:
        self._expiry_timers.pop(topic, None)
        session = self._state.sessions.get(topic)
        if session is None:
            return
        if not session.is_expired():  # extended meanwhile, or the loop clock ran early
            self._arm_expiry(session)
            return
        self._expire_now(topic)

    async def _notify_state_change(self) -> None:
        if self.on_state_change is None:
            return
        try:
            result = self.on_state_change(self._state.model_copy(deep=True))
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            logger.exception("on_state_change callback failed")

    def _schedule_state_change(self) -> None:
        if self.on_state_change is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._notify_state_change(), name="walletconnect-state-change")

    # ------------------------------------------------------------- lifecycle

    async def open(self) -> None:
        """
        Connect to the relay and resume restored sessions (idempotent).

        Subscribes every session topic and fetches messages the relay queued while this
        client was offline. Called automatically by :meth:`connect`, :meth:`request` and
        :meth:`ping`; call it yourself to start receiving wallet messages for restored sessions
        right away.
        """
        async with self._open_lock:
            if self._opened and self.relay.connected:
                return
            self._loop = asyncio.get_running_loop()
            for session in self._expired_on_load:
                self._emit(
                    "session_expire",
                    session.topic,
                    session=session,
                    data={"expiry": session.expiry},
                )
            self._expired_on_load = []
            await self.relay.connect()
            self._prune_expired()
            topics = list(self._state.sessions)
            for topic in topics:
                await self.relay.subscribe(topic)
                self._arm_expiry(self._state.sessions[topic])
            self._opened = True
            if self._maintenance_task is None or self._maintenance_task.done():
                self._maintenance_task = asyncio.create_task(
                    self._maintenance_loop(), name="walletconnect-session-maintenance"
                )
        for topic in topics:
            await self._fetch_pending(topic)

    async def _fetch_pending(self, topic: str) -> None:
        """Drain messages the relay stored for ``topic`` while we were away."""
        for _ in range(_MAX_FETCH_PAGES):
            try:
                messages, has_more = await self.relay.fetch_messages(topic)
            except Exception:  # noqa: BLE001
                logger.debug("fetch_messages failed for %s…", topic[:16], exc_info=True)
                return
            for message in messages:
                await self._on_relay_message(message)
            if not has_more:
                return

    async def connect(
        self,
        *,
        chains: Iterable[int] = (1,),
        methods: Iterable[str] = DEFAULT_EIP155_METHODS,
        events: Iterable[str] = DEFAULT_EIP155_EVENTS,
        optional_namespaces: dict[str, ProposalNamespace | dict[str, Any]] | None = None,
    ) -> tuple[str, asyncio.Task[Session]]:
        """
        Start a session proposal on a new pairing.

        Returns ``(uri, session_task)``. Print/QR the URI, then ``await session_task``
        (or use :meth:`wait_for_session`) until the wallet settles. Several proposals may be
        in flight at once; each has its own pairing topic (``PairingURI.parse(uri).topic``).
        """
        await self.open()

        uri = PairingURI.generate(expiry=PAIRING_PROPOSAL_EXPIRY)
        self._pairing_uri = uri
        self._sym_keys[uri.topic] = uri.sym_key
        await self.relay.subscribe(uri.topic)

        self_key = generate_key_pair()
        namespaces = optional_namespaces
        if namespaces is None:
            namespaces = {
                EIP155_NAMESPACE: ProposalNamespace.eip155(chains, methods, events).to_json()
            }
        else:
            namespaces = {
                k: (v.to_json() if isinstance(v, ProposalNamespace) else v)
                for k, v in namespaces.items()
            }

        params = {
            "relays": [{"protocol": RELAY_PROTOCOL}],
            "proposer": {
                "publicKey": self_key.public_key,
                "metadata": self.metadata.to_json(),
            },
            "requiredNamespaces": {},
            "optionalNamespaces": namespaces,
            "pairingTopic": uri.topic,
            "expiryTimestamp": int(time.time()) + PAIRING_PROPOSAL_EXPIRY,
        }
        request_id = payload_id()
        payload = {
            "id": request_id,
            "jsonrpc": "2.0",
            "method": "wc_sessionPropose",
            "params": params,
        }
        loop = asyncio.get_running_loop()
        propose_fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = propose_fut
        self._proposals[uri.topic] = _Proposal(uri=uri, self_key=self_key, request_id=request_id)

        opts = SIGN_RPC_OPTS["wc_sessionPropose"].req
        envelope = encode_jsonrpc(payload, sym_key=uri.sym_key, type=TYPE_0)
        try:
            await self.relay.publish(uri.topic, envelope, ttl=opts.ttl, tag=opts.tag)
        except BaseException:
            self._pending.pop(request_id, None)
            await self._drop_pairing(uri.topic)
            raise

        session_task = asyncio.create_task(
            self._finish_connect(uri.topic, propose_fut),
            name=f"walletconnect-session-connect-{uri.topic[:8]}",
        )
        return str(uri), session_task

    async def wait_for_session(self, session_task: asyncio.Task[Session]) -> Session:
        return await asyncio.wait_for(session_task, timeout=self.connect_timeout)

    async def _drop_pairing(self, pairing_topic: str) -> None:
        proposal = self._proposals.pop(pairing_topic, None)
        if proposal is not None:
            self._pending.pop(proposal.request_id, None)
        self._sym_keys.pop(pairing_topic, None)
        with contextlib.suppress(Exception):
            if pairing_topic in self.relay.subscriptions:
                await self.relay.unsubscribe(pairing_topic)

    async def _finish_connect(
        self, pairing_topic: str, propose_fut: asyncio.Future[dict[str, Any]]
    ) -> Session:
        proposal = self._proposals[pairing_topic]
        try:
            try:
                result = await asyncio.wait_for(propose_fut, timeout=self.connect_timeout)
            except asyncio.TimeoutError as exc:
                raise WalletConnectTimeoutError(
                    "Timed out waiting for the wallet to approve the session proposal"
                ) from exc

            responder_public_key = result["responderPublicKey"]
            sym_key = derive_sym_key(proposal.self_key.private_key, responder_public_key)
            topic = topic_from_sym_key(sym_key)
            self._sym_keys[topic] = sym_key
            await self.relay.subscribe(topic)

            # Settle may already be buffered (wallet can race the propose response).
            settle_req = self._settle_buffer.pop(topic, None)
            if settle_req is None:
                loop = asyncio.get_running_loop()
                settle_fut: asyncio.Future[dict[str, Any]] = loop.create_future()
                self._settle_waiters[topic] = settle_fut
                try:
                    settle_req = await asyncio.wait_for(settle_fut, timeout=self.connect_timeout)
                except asyncio.TimeoutError as exc:
                    self._sym_keys.pop(topic, None)
                    raise WalletConnectTimeoutError(
                        "Timed out waiting for wc_sessionSettle from the wallet"
                    ) from exc
                finally:
                    self._settle_waiters.pop(topic, None)

            params = settle_req["params"]
            controller = params.get("controller") or {}
            session = Session(
                topic=topic,
                sym_key=sym_key,
                expiry=int(params.get("expiry") or (int(time.time()) + SESSION_EXPIRY)),
                acknowledged=True,
                controller=controller,
                self_public_key=proposal.self_key.public_key,
                peer_public_key=responder_public_key,
                peer_metadata=controller.get("metadata")
                or (params.get("peer") or {}).get("metadata")
                or {},
                namespaces=params.get("namespaces") or {},
                relay_protocol=((params.get("relay") or {}).get("protocol") or RELAY_PROTOCOL),
                pairing_topic=pairing_topic,
            )
            # Ack settle
            await self._send_result(
                topic,
                settle_req["id"],
                True,
                ttl=SIGN_RPC_OPTS["wc_sessionSettle"].res.ttl,
                tag=SIGN_RPC_OPTS["wc_sessionSettle"].res.tag,
            )
            self._state.sessions[topic] = session
            self._arm_expiry(session)
            await self._notify_state_change()
            return session
        finally:
            # The dApp only uses the pairing for this one proposal.
            await self._drop_pairing(pairing_topic)

    # -------------------------------------------------------------- requests

    async def request(
        self,
        method: str,
        params: Any,
        *,
        chain_id: str = "eip155:1",
        topic: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """
        Send a ``wc_sessionRequest`` and wait for the wallet response.

        ``topic`` selects the session; it may be omitted when exactly one session is active.
        """
        session = self._resolve_session(topic)
        await self.open()
        return await self._send_request(
            session,
            "wc_sessionRequest",
            {"request": {"method": method, "params": params}, "chainId": chain_id},
            timeout=timeout or self.request_timeout,
            timeout_message=f"Timed out waiting for wallet response to {method}",
        )

    async def _send_request(
        self,
        session: Session,
        rpc_method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        timeout_message: str,
    ) -> Any:
        """Publish a Sign RPC to the wallet and wait for its response."""
        request_id = payload_id()
        payload = {"id": request_id, "jsonrpc": "2.0", "method": rpc_method, "params": params}
        opts = SIGN_RPC_OPTS[rpc_method].req
        envelope = encode_jsonrpc(payload, sym_key=session.sym_key, type=TYPE_0)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._track(request_id, session.topic, fut)
        try:
            await self.relay.publish(session.topic, envelope, ttl=opts.ttl, tag=opts.tag)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise WalletConnectTimeoutError(timeout_message) from exc
        finally:
            self._untrack(request_id)

    async def personal_sign(
        self,
        message: str | bytes,
        address: str,
        *,
        chain_id: str = "eip155:1",
        topic: str | None = None,
    ) -> str:
        if isinstance(message, str):
            msg_hex = "0x" + message.encode("utf-8").hex()
        else:
            msg_hex = "0x" + message.hex()
        result = await self.request(
            "personal_sign", [msg_hex, address], chain_id=chain_id, topic=topic
        )
        return str(result)

    async def ping(self, *, topic: str | None = None, timeout: float = _PING_TIMEOUT) -> bool:
        """
        Send ``wc_sessionPing`` and wait for the wallet to answer.

        Raises :class:`~walletconnect.exceptions.WalletConnectTimeoutError` (and marks the
        session unreachable) when it doesn't. See :meth:`is_reachable` for a bool version.
        """
        session = self._resolve_session(topic)
        await self.open()
        try:
            await self._send_request(
                session,
                "wc_sessionPing",
                {},
                timeout=timeout,
                timeout_message="Timed out waiting for wallet ping response",
            )
        except WalletConnectTimeoutError:
            if session.topic in self._state.sessions:
                self._unreachable.add(session.topic)
            raise
        self._unreachable.discard(session.topic)
        return True

    async def is_reachable(self, *, topic: str | None = None, timeout: float = 10.0) -> bool:
        """
        Ping the wallet; return ``False`` (and mark the session unreachable) on timeout.

        An unreachable session is **not** deleted: the phone may just be offline or the app
        backgrounded, and the relay keeps queued requests for it. The mark clears as soon as
        any message arrives from the wallet. Relay errors (not the wallet's fault) still raise.
        """
        try:
            return await self.ping(topic=topic, timeout=timeout)
        except WalletConnectTimeoutError:
            return False

    @property
    def unreachable(self) -> frozenset[str]:
        """Topics of sessions whose last ping went unanswered (see :meth:`is_reachable`)."""
        return frozenset(self._unreachable)

    async def extend(self, *, topic: str | None = None, timeout: float | None = None) -> int:
        """
        Ask the wallet to extend the session by 7 days (``wc_sessionExtend``).

        The new expiry (now + 7 days) is applied only once the wallet replies ``true``; returns
        it. Raises :class:`~walletconnect.exceptions.WalletConnectTimeoutError` if the wallet
        doesn't answer within ``timeout`` (the session is left unchanged), or the wallet's
        :class:`~walletconnect.exceptions.SdkError`. Concurrent calls share one request.
        """
        session = self._resolve_session(topic)
        inflight = self._extending.get(session.topic)
        if inflight is None or inflight.done():
            inflight = asyncio.ensure_future(
                self._do_extend(session, timeout or self.request_timeout)
            )
            self._extending[session.topic] = inflight
            inflight.add_done_callback(functools.partial(self._extend_done, session.topic))
        return await asyncio.shield(inflight)

    def _extend_done(self, topic: str, fut: asyncio.Future[int]) -> None:
        if self._extending.get(topic) is fut:
            del self._extending[topic]
        if not fut.cancelled():
            fut.exception()  # retrieved here so an abandoned (shielded) extend doesn't warn

    async def _do_extend(self, session: Session, timeout: float) -> int:
        await self.open()
        result = await self._send_request(
            session,
            "wc_sessionExtend",
            {"expiry": int(time.time()) + SESSION_EXPIRY},
            timeout=timeout,
            timeout_message="Timed out waiting for the wallet to confirm wc_sessionExtend",
        )
        if result is not True:
            raise WalletConnectError(f"Wallet did not confirm wc_sessionExtend: {result!r}")
        current = self._state.sessions.get(session.topic)
        if current is None:
            raise NoActiveSessionError(f"Session {session.topic[:16]}… ended during extend")
        self._apply_extend(current, by="dapp")
        await self._notify_state_change()
        return current.expiry

    def _apply_extend(self, session: Session, *, by: str) -> None:
        new_expiry = int(time.time()) + SESSION_EXPIRY
        if new_expiry <= session.expiry:
            return
        session.expiry = new_expiry
        self._arm_expiry(session)
        self._emit("session_extend", session.topic, data={"expiry": new_expiry, "by": by})

    # ------------------------------------------------------------ maintenance

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await self._maintain()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Session maintenance failed")
            await asyncio.sleep(self.maintenance_interval)

    async def _maintain(self) -> None:
        """One cycle: drop expired sessions, auto-extend, warn about ones about to expire."""
        self._prune_expired()
        await asyncio.gather(
            *(self._maintain_session(t) for t in list(self._state.sessions)),
        )

    async def _maintain_session(self, topic: str) -> None:
        session = self._state.sessions.get(topic)
        if session is None:
            return
        if self.auto_extend and session.expiry - time.time() <= self.auto_extend_threshold:
            try:
                await self.extend(
                    topic=topic, timeout=min(self.request_timeout, self.maintenance_interval)
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info("Auto-extend of session %s… not confirmed yet: %s", topic[:16], exc)
        session = self._state.sessions.get(topic)
        if session is None:
            return
        remaining = session.expiry - time.time()
        if remaining <= self.expiring_warning:
            if self.auto_extend:
                logger.warning(
                    "Session %s… expires in %.1f h and the wallet has not confirmed an extension;"
                    " open the wallet app or pair again",
                    topic[:16],
                    remaining / 3600,
                )
            self._emit(
                "session_expiring",
                topic,
                data={"expiry": session.expiry, "remaining": max(0, int(remaining))},
            )

    async def disconnect(
        self, reason: str = "User disconnected", *, topic: str | None = None
    ) -> None:
        """
        End a session: notify the wallet (``wc_sessionDelete``) and forget it.

        With no ``topic`` this ends the only active session (no-op when there is none).
        The relay connection stays open; call :meth:`close` to shut it down.
        """
        if topic is None and not self.sessions:
            return
        session = self._resolve_session(topic)
        request_id = payload_id()
        payload = {
            "id": request_id,
            "jsonrpc": "2.0",
            "method": "wc_sessionDelete",
            "params": {"code": 6000, "message": reason},
        }
        opts = SIGN_RPC_OPTS["wc_sessionDelete"].req
        envelope = encode_jsonrpc(payload, sym_key=session.sym_key, type=TYPE_0)
        try:
            await self.open()
            await self.relay.publish(session.topic, envelope, ttl=opts.ttl, tag=opts.tag)
        except Exception:  # noqa: BLE001
            logger.debug("sessionDelete publish failed", exc_info=True)
        self._remove_session(session.topic)
        with contextlib.suppress(Exception):
            await self.relay.unsubscribe(session.topic)
        await self._notify_state_change()

    async def close(self) -> None:
        """Close the relay connection. In-memory state is kept (see :meth:`export_state`)."""
        self._opened = False
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._maintenance_task
            self._maintenance_task = None
        for timer in self._expiry_timers.values():
            timer.cancel()
        self._expiry_timers.clear()
        await self.relay.close()

    async def __aenter__(self) -> SignClient:  # noqa: PYI034
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --------------------------------------------------------------- inbound

    async def _send_result(
        self, topic: str, request_id: int, result: Any, *, ttl: int, tag: int
    ) -> None:
        await self._send_response(
            topic, {"id": request_id, "jsonrpc": "2.0", "result": result}, ttl=ttl, tag=tag
        )

    async def _send_error(
        self, topic: str, request_id: int, error: SdkError, *, ttl: int, tag: int
    ) -> None:
        payload = {"id": request_id, "jsonrpc": "2.0", "error": error.to_json()}
        await self._send_response(topic, payload, ttl=ttl, tag=tag)

    async def _send_response(
        self, topic: str, payload: dict[str, Any], *, ttl: int, tag: int
    ) -> None:
        sym_key = self._sym_keys[topic]
        envelope = encode_jsonrpc(payload, sym_key=sym_key, type=TYPE_0)
        await self.relay.publish(topic, envelope, ttl=ttl, tag=tag)

    def _first_sighting(self, message: RelayMessage) -> bool:
        """De-duplicate messages delivered both by subscription and by fetchMessages."""
        digest = hashlib.sha256(f"{message.topic}:{message.message}".encode()).hexdigest()
        if digest in self._seen:
            return False
        self._seen[digest] = None
        if len(self._seen) > _SEEN_CACHE_SIZE:
            self._seen.popitem(last=False)
        return True

    async def _on_relay_message(self, message: RelayMessage) -> None:
        sym_key = self._sym_keys.get(message.topic)
        if not sym_key:
            logger.debug("No sym key for topic %s", message.topic[:16])
            return
        if not self._first_sighting(message):
            return
        try:
            payload = decode_jsonrpc(message.message, sym_key=sym_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to decrypt relay message on %s", message.topic[:16], exc_info=True
            )
            return

        # Any message from the wallet proves it is reachable again.
        self._unreachable.discard(message.topic)
        if "method" in payload:
            await self._handle_peer_request(message.topic, payload)
            return

        request_id = payload.get("id")
        if request_id is None:
            return
        try:
            pending_key = coerce_rpc_id(request_id)
        except ValueError:
            logger.debug("Ignoring message with invalid id: %r", request_id)
            return
        fut = self._pending.get(pending_key)
        if fut is None or fut.done():
            return
        if "error" in payload:
            err = payload["error"] or {}
            fut.set_exception(
                SdkError.from_code(
                    int(err.get("code", 0)),
                    err.get("message"),
                    err.get("data"),
                )
            )
        else:
            fut.set_result(payload.get("result"))

    async def _handle_peer_request(self, topic: str, payload: dict[str, Any]) -> None:
        method = payload.get("method")
        params = payload.get("params") or {}
        if method == "wc_sessionSettle":
            waiter = self._settle_waiters.get(topic)
            if waiter is not None and not waiter.done():
                waiter.set_result(payload)
            else:
                self._settle_buffer[topic] = payload
            return
        if method == "wc_sessionPing":
            await self._send_result(
                topic,
                payload["id"],
                True,
                ttl=SIGN_RPC_OPTS["wc_sessionPing"].res.ttl,
                tag=SIGN_RPC_OPTS["wc_sessionPing"].res.tag,
            )
            return
        if method == "wc_sessionDelete":
            with contextlib.suppress(Exception):
                await self._send_result(
                    topic,
                    payload["id"],
                    True,
                    ttl=SIGN_RPC_OPTS["wc_sessionDelete"].res.ttl,
                    tag=SIGN_RPC_OPTS["wc_sessionDelete"].res.tag,
                )
            session = self._remove_session(topic)
            if session is not None:
                code, reason = params.get("code"), params.get("message")
                logger.info("Wallet deleted session %s…: %s", topic[:16], reason)
                self._after_session_gone(
                    session,
                    "session_delete",
                    {"code": code, "message": reason},
                    lambda: SessionDeletedError(
                        f"The wallet deleted session {topic[:16]}…: {reason}",
                        code=code,
                        reason=reason,
                    ),
                )
                await self._notify_state_change()
            return
        if method == "wc_sessionUpdate":
            await self._handle_update(topic, payload)
            return
        if method in {"wc_sessionExtend", "wc_sessionEvent"}:
            res = SIGN_RPC_OPTS[method].res
            await self._send_result(topic, payload["id"], True, ttl=res.ttl, tag=res.tag)
            session = self._state.sessions.get(topic)
            if session is None:
                return
            if method == "wc_sessionExtend":
                # Like the JS SDK: a wallet-sent extend means "7 more days from now".
                self._apply_extend(session, by="wallet")
                await self._notify_state_change()
            else:
                event = params.get("event") or {}
                logger.debug("Session event on %s…: %r", topic[:16], event)
                self._emit(
                    "session_event",
                    topic,
                    data={
                        "name": event.get("name"),
                        "data": event.get("data"),
                        "chain_id": params.get("chainId"),
                    },
                )
            return
        logger.debug("Unhandled peer method %s", method)

    async def _handle_update(self, topic: str, payload: dict[str, Any]) -> None:
        """Apply ``wc_sessionUpdate``: controller only, newest request id wins."""
        res = SIGN_RPC_OPTS["wc_sessionUpdate"].res
        session = self._state.sessions.get(topic)
        if session is None:
            return
        params = payload.get("params") or {}
        namespaces = params.get("namespaces")
        try:
            request_id = coerce_rpc_id(payload["id"])
        except (KeyError, ValueError):
            return
        controller_key = (session.controller or {}).get("publicKey")
        last = session.last_update_id
        error: SdkError | None = None
        if controller_key and session.peer_public_key and controller_key != session.peer_public_key:
            error = UnauthorizedUpdateRequestError("Only the session controller may update it")
        elif not isinstance(namespaces, dict) or not namespaces:
            error = InvalidUpdateRequestError("wc_sessionUpdate needs non-empty namespaces")
        elif last is not None and request_id < last:
            error = InvalidUpdateRequestError("Stale wc_sessionUpdate (a newer one was applied)")
        if error is not None:
            logger.info("Rejected wc_sessionUpdate on %s…: %s", topic[:16], error)
            await self._send_error(topic, payload["id"], error, ttl=res.ttl, tag=res.tag)
            return
        await self._send_result(topic, payload["id"], True, ttl=res.ttl, tag=res.tag)
        if request_id == last:
            return  # redelivered (e.g. fetched again after a restart): already applied
        session.namespaces = namespaces  # type: ignore[assignment]
        session.last_update_id = request_id
        self._emit("session_update", topic, data={"namespaces": namespaces})
        await self._notify_state_change()
