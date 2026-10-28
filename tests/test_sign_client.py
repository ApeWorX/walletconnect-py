"""Offline SignClient integration against the fake relay + a scripted wallet peer."""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.fake_relay import run_fake_relay
from walletconnect.constants import SIGN_RPC_OPTS
from walletconnect.crypto.codec import decode_jsonrpc, encode_jsonrpc
from walletconnect.crypto.envelope import TYPE_0
from walletconnect.crypto.keys import derive_sym_key, generate_key_pair, topic_from_sym_key
from walletconnect.pairing.uri import PairingURI
from walletconnect.relay.client import RelayClient
from walletconnect.sign.client import SignClient
from walletconnect.storage import KeyValueStorage
from walletconnect.types import Metadata
from walletconnect.utils.ids import payload_id


async def scripted_wallet(
    relay_url: str,
    uri_str: str,
    *,
    project_id: str = "test",
    account: str = "eip155:1:0x1111111111111111111111111111111111111111",
) -> None:
    """Minimal wallet: answer propose, settle, then answer personal_sign."""
    uri = PairingURI.parse(uri_str)
    wallet_key = generate_key_pair()
    async with RelayClient(project_id=project_id, relay_url=relay_url) as wallet:
        propose_box: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

        async def on_msg(message) -> None:
            if message.topic != uri.topic or propose_box.done():
                return
            payload = decode_jsonrpc(message.message, sym_key=uri.sym_key)
            if payload.get("method") == "wc_sessionPropose":
                propose_box.set_result(payload)

        wallet.on_message(on_msg)
        await wallet.subscribe(uri.topic)
        propose = await asyncio.wait_for(propose_box, timeout=5)
        proposer_pub = propose["params"]["proposer"]["publicKey"]

        sym_key = derive_sym_key(wallet_key.private_key, proposer_pub)
        session_topic = topic_from_sym_key(sym_key)
        response = {
            "id": propose["id"],
            "jsonrpc": "2.0",
            "result": {
                "relay": {"protocol": "irn"},
                "responderPublicKey": wallet_key.public_key,
            },
        }
        opts = SIGN_RPC_OPTS["wc_sessionPropose"].res
        await wallet.publish(
            uri.topic,
            encode_jsonrpc(response, sym_key=uri.sym_key, type=TYPE_0),
            ttl=opts.ttl,
            tag=opts.tag,
        )

        done = asyncio.Event()

        async def on_session(message) -> None:
            if message.topic != session_topic:
                return
            payload = decode_jsonrpc(message.message, sym_key=sym_key)
            if payload.get("method") == "wc_sessionRequest":
                result = {
                    "id": payload["id"],
                    "jsonrpc": "2.0",
                    "result": "0xdeadbeef",
                }
                ropts = SIGN_RPC_OPTS["wc_sessionRequest"].res
                await wallet.publish(
                    session_topic,
                    encode_jsonrpc(result, sym_key=sym_key, type=TYPE_0),
                    ttl=ropts.ttl,
                    tag=ropts.tag,
                )
                done.set()

        wallet.on_message(on_session)
        await wallet.subscribe(session_topic)

        settle_id = payload_id()
        settle = {
            "id": settle_id,
            "jsonrpc": "2.0",
            "method": "wc_sessionSettle",
            "params": {
                "relay": {"protocol": "irn"},
                "controller": {
                    "publicKey": wallet_key.public_key,
                    "metadata": {
                        "name": "FakeWallet",
                        "description": "",
                        "url": "",
                        "icons": [],
                    },
                },
                "namespaces": {
                    "eip155": {
                        "accounts": [account],
                        "methods": ["personal_sign", "eth_sendTransaction"],
                        "events": ["chainChanged", "accountsChanged"],
                    }
                },
                "expiry": int(time.time()) + 60 * 60,
            },
        }
        sopts = SIGN_RPC_OPTS["wc_sessionSettle"].req
        await wallet.publish(
            session_topic,
            encode_jsonrpc(settle, sym_key=sym_key, type=TYPE_0),
            ttl=sopts.ttl,
            tag=sopts.tag,
        )

        await asyncio.wait_for(done.wait(), timeout=10)


@pytest.mark.asyncio
async def test_connect_and_personal_sign(tmp_path):
    async with run_fake_relay() as (_fake, base_url):
        client = SignClient(
            project_id="test",
            relay_url=base_url,
            storage=KeyValueStorage(tmp_path),
            metadata=Metadata(name="test-dapp", description="", url="", icons=[]),
            connect_timeout=10,
            request_timeout=10,
        )
        uri, task = await client.connect(chains=(1,))
        wallet_task = asyncio.create_task(scripted_wallet(base_url, uri))
        session = await client.wait_for_session(task)
        assert session.eip155_accounts
        address = session.eip155_accounts[0].split(":")[-1]
        sig = await client.personal_sign("hi", address)
        assert sig == "0xdeadbeef"
        await wallet_task
        await client.disconnect()
