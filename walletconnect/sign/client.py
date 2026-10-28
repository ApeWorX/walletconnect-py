"""
Async Sign API client (dApp = proposer).

Flow: connect → print pairing URI/QR → wallet settles session → request signatures.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from walletconnect.constants import (
    DEFAULT_EIP155_EVENTS,
    DEFAULT_EIP155_METHODS,
    DEFAULT_RELAY_URL,
    EIP155_NAMESPACE,
    PAIRING_PROPOSAL_EXPIRY,
    RELAY_PROTOCOL,
    SESSION_EXPIRY,
    SIGN_RPC_OPTS,
)
from walletconnect.crypto.codec import decode_jsonrpc, encode_jsonrpc
from walletconnect.crypto.envelope import TYPE_0
from walletconnect.crypto.jwt import AuthKeyPair, generate_auth_key_pair
from walletconnect.crypto.keys import derive_sym_key, generate_key_pair, topic_from_sym_key
from walletconnect.exceptions import (
    SdkError,
    WalletConnectError,
    WalletConnectTimeoutError,
)
from walletconnect.pairing.uri import PairingURI
from walletconnect.relay.client import RelayClient, RelayMessage
from walletconnect.storage.kv import KeyValueStorage
from walletconnect.types import Metadata, ProposalNamespace
from walletconnect.utils.ids import payload_id
from walletconnect.utils.rpc_id import coerce_rpc_id

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 300.0  # 5 minutes for the user to scan + approve
_REQUEST_TIMEOUT = 300.0


@dataclass
class Session:
    """A settled WalletConnect session."""

    topic: str
    sym_key: str
    expiry: int
    acknowledge: bool = False
    controller: dict[str, Any] = field(default_factory=dict)
    self_public_key: str = ""
    peer_public_key: str = ""
    peer_metadata: dict[str, Any] = field(default_factory=dict)
    namespaces: dict[str, Any] = field(default_factory=dict)
    relay_protocol: str = RELAY_PROTOCOL

    @property
    def accounts(self) -> list[str]:
        accounts: list[str] = []
        for ns in self.namespaces.values():
            accounts.extend(ns.get("accounts") or [])
        return accounts

    @property
    def eip155_accounts(self) -> list[str]:
        ns = self.namespaces.get(EIP155_NAMESPACE) or {}
        return list(ns.get("accounts") or [])

    def to_json(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "symKey": self.sym_key,
            "expiry": self.expiry,
            "acknowledged": self.acknowledge,
            "controller": self.controller,
            "selfPublicKey": self.self_public_key,
            "peerPublicKey": self.peer_public_key,
            "peerMetadata": self.peer_metadata,
            "namespaces": self.namespaces,
            "relayProtocol": self.relay_protocol,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Session:
        return cls(
            topic=data["topic"],
            sym_key=data["symKey"],
            expiry=int(data["expiry"]),
            acknowledge=bool(data.get("acknowledged", True)),
            controller=data.get("controller") or {},
            self_public_key=data.get("selfPublicKey") or "",
            peer_public_key=data.get("peerPublicKey") or "",
            peer_metadata=data.get("peerMetadata") or {},
            namespaces=data.get("namespaces") or {},
            relay_protocol=data.get("relayProtocol") or RELAY_PROTOCOL,
        )


class SignClient:
    """Async dApp-side WalletConnect Sign client."""

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
        self.metadata = (
            metadata
            if isinstance(metadata, Metadata)
            else Metadata.model_validate(
                metadata
                or {
                    "name": "walletconnect-py",
                    "description": "Python WalletConnect v2 client",
                    "url": "https://github.com/ApeWorX/walletconnect-py",
                    "icons": [],
                }
            )
        )
        self.storage = storage or KeyValueStorage()
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout

        stored_seed = self.storage.get("client_seed")
        if auth_key is not None:
            self.auth_key = auth_key
        elif stored_seed:
            self.auth_key = generate_auth_key_pair(stored_seed)
        else:
            self.auth_key = generate_auth_key_pair()
            self.storage.set("client_seed", self.auth_key.private_key)

        self.relay = RelayClient(
            project_id,
            relay_url=relay_url,
            auth_key=self.auth_key,
            origin=origin,
            request_timeout=max(30.0, min(request_timeout, 60.0)),
        )
        self.relay.on_message(self._on_relay_message)

        self.session: Session | None = None
        self._sym_keys: dict[str, str] = {}  # topic -> symKey
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._settle_buffer: dict[str, dict[str, Any]] = {}  # topic -> settle request
        self._settle_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pairing_uri: PairingURI | None = None
        self._self_key = generate_key_pair()  # ephemeral proposer key for current connect

    @property
    def pairing_uri(self) -> str | None:
        return str(self._pairing_uri) if self._pairing_uri else None

    async def connect(
        self,
        *,
        chains: Iterable[int] = (1,),
        methods: Iterable[str] = DEFAULT_EIP155_METHODS,
        events: Iterable[str] = DEFAULT_EIP155_EVENTS,
        optional_namespaces: dict[str, ProposalNamespace | dict[str, Any]] | None = None,
    ) -> tuple[str, asyncio.Task[Session]]:
        """
        Start a session proposal.

        Returns ``(uri, session_task)``. Print/QR the URI, then ``await session_task``
        (or use :meth:`wait_for_session`) until the wallet settles.
        """
        await self.relay.connect()

        uri = PairingURI.generate(expiry=PAIRING_PROPOSAL_EXPIRY)
        self._pairing_uri = uri
        self._sym_keys[uri.topic] = uri.sym_key
        await self.relay.subscribe(uri.topic)

        self._self_key = generate_key_pair()
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
                "publicKey": self._self_key.public_key,
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
        opts = SIGN_RPC_OPTS["wc_sessionPropose"].req
        envelope = encode_jsonrpc(payload, sym_key=uri.sym_key, type=TYPE_0)
        await self.relay.publish(uri.topic, envelope, ttl=opts.ttl, tag=opts.tag)

        loop = asyncio.get_running_loop()
        propose_fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = propose_fut

        session_task = asyncio.create_task(
            self._finish_connect(propose_fut),
            name="walletconnect-session-connect",
        )
        return str(uri), session_task

    async def wait_for_session(self, session_task: asyncio.Task[Session]) -> Session:
        return await asyncio.wait_for(session_task, timeout=self.connect_timeout)

    async def _finish_connect(self, propose_fut: asyncio.Future[dict[str, Any]]) -> Session:
        try:
            result = await asyncio.wait_for(propose_fut, timeout=self.connect_timeout)
        except TimeoutError as exc:
            raise WalletConnectTimeoutError(
                "Timed out waiting for the wallet to approve the session proposal"
            ) from exc

        responder_public_key = result["responderPublicKey"]
        sym_key = derive_sym_key(self._self_key.private_key, responder_public_key)
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
            except TimeoutError as exc:
                raise WalletConnectTimeoutError(
                    "Timed out waiting for wc_sessionSettle from the wallet"
                ) from exc
            finally:
                self._settle_waiters.pop(topic, None)

        params = settle_req["params"]
        session = Session(
            topic=topic,
            sym_key=sym_key,
            expiry=int(params.get("expiry") or (int(time.time()) + SESSION_EXPIRY)),
            acknowledge=True,
            controller=params.get("controller") or {},
            self_public_key=self._self_key.public_key,
            peer_public_key=responder_public_key,
            peer_metadata=(params.get("controller") or {}).get("metadata")
            or (params.get("peer") or {}).get("metadata")
            or {},
            namespaces=params.get("namespaces") or {},
            relay_protocol=((params.get("relay") or {}).get("protocol") or RELAY_PROTOCOL),
        )
        # Ack settle
        await self._send_result(
            topic,
            settle_req["id"],
            True,
            ttl=SIGN_RPC_OPTS["wc_sessionSettle"].res.ttl,
            tag=SIGN_RPC_OPTS["wc_sessionSettle"].res.tag,
        )
        self.session = session
        self.storage.set("session", session.to_json())
        self.storage.set(f"key:{topic}", sym_key)
        return session

    async def request(
        self,
        method: str,
        params: Any,
        *,
        chain_id: str = "eip155:1",
        timeout: float | None = None,
    ) -> Any:
        """Send a ``wc_sessionRequest`` and wait for the wallet response."""
        if self.session is None:
            raise WalletConnectError("No active session; call connect() first")
        session = self.session
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
        except TimeoutError as exc:
            raise WalletConnectTimeoutError(
                f"Timed out waiting for wallet response to {method}"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def personal_sign(
        self, message: str | bytes, address: str, *, chain_id: str = "eip155:1"
    ) -> str:
        if isinstance(message, str):
            msg_hex = "0x" + message.encode("utf-8").hex()
        else:
            msg_hex = "0x" + message.hex()
        result = await self.request("personal_sign", [msg_hex, address], chain_id=chain_id)
        return str(result)

    async def ping(self) -> bool:
        if self.session is None:
            raise WalletConnectError("No active session")
        request_id = payload_id()
        payload = {"id": request_id, "jsonrpc": "2.0", "method": "wc_sessionPing", "params": {}}
        opts = SIGN_RPC_OPTS["wc_sessionPing"].req
        envelope = encode_jsonrpc(payload, sym_key=self.session.sym_key, type=TYPE_0)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = fut
        try:
            await self.relay.publish(self.session.topic, envelope, ttl=opts.ttl, tag=opts.tag)
            await asyncio.wait_for(fut, timeout=30)
            return True
        finally:
            self._pending.pop(request_id, None)

    async def disconnect(self, reason: str = "User disconnected") -> None:
        if self.session is None:
            await self.relay.close()
            return
        session = self.session
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
            await self.relay.publish(session.topic, envelope, ttl=opts.ttl, tag=opts.tag)
        except Exception:  # noqa: BLE001
            logger.debug("sessionDelete publish failed", exc_info=True)
        self.storage.delete("session")
        self.storage.delete(f"key:{session.topic}")
        self.session = None
        await self.relay.close()

    async def close(self) -> None:
        await self.relay.close()

    async def __aenter__(self) -> SignClient:  # noqa: PYI034
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _send_result(
        self, topic: str, request_id: int, result: Any, *, ttl: int, tag: int
    ) -> None:
        sym_key = self._sym_keys[topic]
        payload = {"id": request_id, "jsonrpc": "2.0", "result": result}
        envelope = encode_jsonrpc(payload, sym_key=sym_key, type=TYPE_0)
        await self.relay.publish(topic, envelope, ttl=ttl, tag=tag)

    async def _on_relay_message(self, message: RelayMessage) -> None:
        sym_key = self._sym_keys.get(message.topic)
        if sym_key is None:
            # Try session key
            if self.session and self.session.topic == message.topic:
                sym_key = self.session.sym_key
            else:
                stored = self.storage.get(f"key:{message.topic}")
                if stored:
                    sym_key = stored
                    self._sym_keys[message.topic] = stored
        if not sym_key:
            logger.debug("No sym key for topic %s", message.topic[:16])
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
            if self.session and self.session.topic == topic:
                self.storage.delete("session")
                self.storage.delete(f"key:{topic}")
                self.session = None
            with contextlib.suppress(Exception):
                await self._send_result(
                    topic,
                    payload["id"],
                    True,
                    ttl=SIGN_RPC_OPTS["wc_sessionDelete"].res.ttl,
                    tag=SIGN_RPC_OPTS["wc_sessionDelete"].res.tag,
                )
            return
        if method in {"wc_sessionUpdate", "wc_sessionExtend", "wc_sessionEvent"}:
            # Ack and apply lightly.
            res = SIGN_RPC_OPTS[method].res
            await self._send_result(topic, payload["id"], True, ttl=res.ttl, tag=res.tag)
            if method == "wc_sessionUpdate" and self.session and self.session.topic == topic:
                namespaces = (payload.get("params") or {}).get("namespaces")
                if namespaces:
                    self.session.namespaces = namespaces
                    self.storage.set("session", self.session.to_json())
            if method == "wc_sessionExtend" and self.session and self.session.topic == topic:
                expiry = (payload.get("params") or {}).get("expiry")
                if expiry:
                    self.session.expiry = int(expiry)
                    self.storage.set("session", self.session.to_json())
            return
        logger.debug("Unhandled peer method %s", method)
