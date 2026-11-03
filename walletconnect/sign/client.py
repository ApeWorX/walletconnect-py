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
    NoActiveSessionError,
    SdkError,
    SessionExpiredError,
    WalletConnectTimeoutError,
)
from walletconnect.pairing.uri import PairingURI
from walletconnect.relay.client import RelayClient, RelayMessage
from walletconnect.types import Metadata, ProposalNamespace
from walletconnect.utils.ids import payload_id
from walletconnect.utils.rpc_id import coerce_rpc_id

from .state import ClientState, Session

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 300.0  # 5 minutes for the user to scan + approve
_REQUEST_TIMEOUT = 300.0
_SEEN_CACHE_SIZE = 2048
_MAX_FETCH_PAGES = 20

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
    ) -> None:
        self.metadata = _resolve_metadata(metadata)
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.on_state_change = on_state_change

        self._state = state.model_copy(deep=True) if state is not None else ClientState()
        if dropped := self._state.prune_expired():
            logger.info("Dropped %d expired session(s) from restored state", len(dropped))
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
        expired = self._state.prune_expired()
        for topic in expired:
            self._sym_keys.pop(topic, None)
            logger.info("Session %s… expired", topic[:16])
        if expired and notify:
            self._schedule_state_change()
        return expired

    def _resolve_session(self, topic: str | None) -> Session:
        if topic is not None:
            session = self._state.sessions.get(topic)
            if session is None:
                raise NoActiveSessionError(f"No session for topic {topic[:16]}…")
            if session.is_expired():
                self._remove_session(topic)
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
        return session

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
            await self.relay.connect()
            self._prune_expired()
            topics = list(self._state.sessions)
            for topic in topics:
                await self.relay.subscribe(topic)
            self._opened = True
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
        request_id = payload_id()
        payload = {
            "id": request_id,
            "jsonrpc": "2.0",
            "method": "wc_sessionRequest",
            "params": {
                "request": {"method": method, "params": params},
                "chainId": chain_id,
            },
        }
        opts = SIGN_RPC_OPTS["wc_sessionRequest"].req
        envelope = encode_jsonrpc(payload, sym_key=session.sym_key, type=TYPE_0)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = fut
        try:
            await self.relay.publish(session.topic, envelope, ttl=opts.ttl, tag=opts.tag)
            return await asyncio.wait_for(fut, timeout=timeout or self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise WalletConnectTimeoutError(
                f"Timed out waiting for wallet response to {method}"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

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

    async def ping(self, *, topic: str | None = None, timeout: float = 30.0) -> bool:
        """Send ``wc_sessionPing`` and wait for the wallet to answer."""
        session = self._resolve_session(topic)
        await self.open()
        request_id = payload_id()
        payload = {"id": request_id, "jsonrpc": "2.0", "method": "wc_sessionPing", "params": {}}
        opts = SIGN_RPC_OPTS["wc_sessionPing"].req
        envelope = encode_jsonrpc(payload, sym_key=session.sym_key, type=TYPE_0)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = fut
        try:
            await self.relay.publish(session.topic, envelope, ttl=opts.ttl, tag=opts.tag)
            await asyncio.wait_for(fut, timeout=timeout)
            return True
        except asyncio.TimeoutError as exc:
            raise WalletConnectTimeoutError("Timed out waiting for wallet ping response") from exc
        finally:
            self._pending.pop(request_id, None)

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
        sym_key = self._sym_keys[topic]
        payload = {"id": request_id, "jsonrpc": "2.0", "result": result}
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
            if self._remove_session(topic) is not None:
                logger.info("Wallet deleted session %s…: %s", topic[:16], params.get("message"))
                with contextlib.suppress(Exception):
                    await self.relay.unsubscribe(topic)
                await self._notify_state_change()
            return
        if method in {"wc_sessionUpdate", "wc_sessionExtend", "wc_sessionEvent"}:
            res = SIGN_RPC_OPTS[method].res
            await self._send_result(topic, payload["id"], True, ttl=res.ttl, tag=res.tag)
            session = self._state.sessions.get(topic)
            if session is None:
                return
            if method == "wc_sessionUpdate" and params.get("namespaces"):
                session.namespaces = params["namespaces"]
                await self._notify_state_change()
            elif method == "wc_sessionExtend" and params.get("expiry"):
                session.expiry = int(params["expiry"])
                await self._notify_state_change()
            elif method == "wc_sessionEvent":
                logger.debug("Session event on %s…: %r", topic[:16], params.get("event"))
            return
        logger.debug("Unhandled peer method %s", method)
