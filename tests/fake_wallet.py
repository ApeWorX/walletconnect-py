"""A scriptable fake wallet for session-lifecycle tests (talks to the fake relay)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from walletconnect.constants import SIGN_RPC_OPTS
from walletconnect.crypto.codec import decode_jsonrpc, encode_jsonrpc
from walletconnect.crypto.envelope import TYPE_0
from walletconnect.crypto.keys import derive_sym_key, generate_key_pair, topic_from_sym_key
from walletconnect.pairing.uri import PairingURI
from walletconnect.relay import RelayClient
from walletconnect.utils.ids import payload_id

ACCOUNT = "eip155:1:0x" + "11" * 20
NOT_ANSWERED = object()


class FakeWallet:
    """
    Pairs with a dApp URI, settles a session, then answers dApp requests from ``answers``
    (method -> result, or a callable ``payload -> result``; missing methods or
    ``NOT_ANSWERED`` get no reply). ``send()`` publishes wallet-initiated requests.
    """

    def __init__(
        self,
        relay_url: str,
        uri: str,
        *,
        account: str = ACCOUNT,
        expiry: int | None = None,
        controller_key: str | None = None,
    ) -> None:
        self.relay_url = relay_url
        self.uri = PairingURI.parse(uri)
        self.account = account
        self.expiry = expiry if expiry is not None else int(time.time()) + 3600
        self.key = generate_key_pair()
        self.controller_key = controller_key or self.key.public_key
        self.answers: dict[str, Any] = {
            "wc_sessionPing": True,
            "wc_sessionExtend": True,
            "wc_sessionRequest": "0xdeadbeef",
        }
        self.received: list[dict[str, Any]] = []
        self.relay = RelayClient(project_id="test", relay_url=relay_url)
        self.topic = ""
        self.sym_key = ""
        self._responses: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._propose: asyncio.Future[dict[str, Any]] | None = None

    async def start(self) -> FakeWallet:
        loop = asyncio.get_running_loop()
        self._propose = loop.create_future()
        self.relay.on_message(self._on_message)
        await self.relay.connect()
        await self.relay.subscribe(self.uri.topic)
        propose = await asyncio.wait_for(self._propose, 5)
        self.sym_key = derive_sym_key(
            self.key.private_key, propose["params"]["proposer"]["publicKey"]
        )
        self.topic = topic_from_sym_key(self.sym_key)
        await self.relay.subscribe(self.topic)
        res = {
            "id": propose["id"],
            "jsonrpc": "2.0",
            "result": {"relay": {"protocol": "irn"}, "responderPublicKey": self.key.public_key},
        }
        opts = SIGN_RPC_OPTS["wc_sessionPropose"].res
        await self.relay.publish(
            self.uri.topic,
            encode_jsonrpc(res, sym_key=self.uri.sym_key, type=TYPE_0),
            ttl=opts.ttl,
            tag=opts.tag,
        )
        settle = {
            "relay": {"protocol": "irn"},
            "controller": {
                "publicKey": self.controller_key,
                "metadata": {"name": "FakeWallet", "description": "", "url": "", "icons": []},
            },
            "namespaces": {
                "eip155": {
                    "accounts": [self.account],
                    "methods": ["personal_sign", "eth_sendTransaction"],
                    "events": ["chainChanged", "accountsChanged"],
                }
            },
            "expiry": self.expiry,
        }
        await self.send("wc_sessionSettle", settle)
        return self

    async def _on_message(self, message: Any) -> None:
        if message.topic == self.uri.topic and self._propose and not self._propose.done():
            payload = decode_jsonrpc(message.message, sym_key=self.uri.sym_key)
            if payload.get("method") == "wc_sessionPropose":
                self._propose.set_result(payload)
            return
        if not self.topic or message.topic != self.topic:
            return
        payload = decode_jsonrpc(message.message, sym_key=self.sym_key)
        if "method" not in payload:
            fut = self._responses.get(int(payload["id"]))
            if fut is not None and not fut.done():
                fut.set_result(payload)
            return
        if any(r["id"] == payload["id"] for r in self.received):
            return  # redelivered
        self.received.append(payload)
        answer = self.answers.get(payload["method"], NOT_ANSWERED)
        if callable(answer):
            answer = answer(payload)
        if answer is NOT_ANSWERED:
            return
        body: dict[str, Any] = {"id": payload["id"], "jsonrpc": "2.0"}
        if isinstance(answer, dict) and "code" in answer:
            body["error"] = answer
        else:
            body["result"] = answer
        opts = SIGN_RPC_OPTS[payload["method"]].res
        await self.relay.publish(
            self.topic,
            encode_jsonrpc(body, sym_key=self.sym_key, type=TYPE_0),
            ttl=opts.ttl,
            tag=opts.tag,
        )

    async def send(
        self, method: str, params: Any, *, request_id: int | None = None, timeout: float = 5
    ) -> dict[str, Any]:
        """Publish a wallet request on the session topic and return the dApp's response."""
        request_id = request_id if request_id is not None else payload_id()
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._responses[request_id] = fut
        body = {"id": request_id, "jsonrpc": "2.0", "method": method, "params": params}
        opts = SIGN_RPC_OPTS[method].req
        await self.relay.publish(
            self.topic,
            encode_jsonrpc(body, sym_key=self.sym_key, type=TYPE_0),
            ttl=opts.ttl,
            tag=opts.tag,
        )
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._responses.pop(request_id, None)

    def requests(self, method: str) -> list[dict[str, Any]]:
        return [r for r in self.received if r["method"] == method]

    async def close(self) -> None:
        await self.relay.close()
