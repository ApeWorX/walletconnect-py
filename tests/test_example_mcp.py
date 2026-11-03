"""Smoke test for examples/mcp_server: drive the tools in-process against the fake relay."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from examples.mcp_server.server import METADATA, create_server  # noqa: E402
from tests.fake_relay import run_fake_relay  # noqa: E402
from tests.fake_wallet import FakeWallet  # noqa: E402
from walletconnect.sign import SignClient  # noqa: E402

TYPED = {
    "types": {"EIP712Domain": [{"name": "name", "type": "string"}]},
    "primaryType": "EIP712Domain",
    "domain": {"name": "test"},
    "message": {},
}
ADDRESS = "0x" + "11" * 20


async def _call(mcp: Client, tool: str, **arguments) -> dict:
    result = await mcp.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content  # type: ignore[no-any-return]


async def test_mcp_example_end_to_end():
    async with run_fake_relay() as (_fake, url):

        def factory() -> SignClient:
            return SignClient(
                project_id="test",
                relay_url=url,
                metadata=METADATA,
                connect_timeout=10,
                request_timeout=10,
            )

        async with Client(create_server(factory)) as mcp:
            tools = {tool.name for tool in (await mcp.list_tools()).tools}
            assert tools == {
                "pair",
                "session_status",
                "sign_message",
                "sign_typed_data",
                "send_transaction",
            }
            assert await _call(mcp, "session_status") == {"sessions": []}

            no_session = await mcp.call_tool("sign_message", {"message": "hi"})
            assert no_session.is_error
            assert "pair" in no_session.content[0].text

            pair = await _call(mcp, "pair", qr="svg")
            assert pair["uri"].startswith("wc:")
            assert pair["qr"] is None or pair["qr"].startswith("<svg")
            wallet = await FakeWallet(url, pair["uri"]).start()
            try:
                for _ in range(200):
                    status = await _call(mcp, "session_status", pairing_topic=pair["pairing_topic"])
                    if status["pairing"]["status"] != "pending":
                        break
                    await asyncio.sleep(0.05)
                assert status["pairing"]["status"] == "settled", status
                assert status["pairing"]["session_topic"] == wallet.topic
                [session] = status["sessions"]
                assert session["peer_name"] == "FakeWallet"
                assert session["reachable"] is True

                signed = await _call(mcp, "sign_message", message="hello")
                assert signed == {"address": ADDRESS, "signature": "0xdeadbeef"}
                assert wallet.received[-1]["params"]["request"]["method"] == "personal_sign"

                signed = await _call(mcp, "sign_typed_data", typed_data=TYPED)
                assert signed["signature"] == "0xdeadbeef"
                request = wallet.received[-1]["params"]["request"]
                assert request["method"] == "eth_signTypedData_v4"
                assert json.loads(request["params"][1]) == TYPED

                sent = await _call(
                    mcp, "send_transaction", to="0x" + "22" * 20, value_wei=1, data="0x8456cb59"
                )
                assert sent == {"from": ADDRESS, "chain_id": "eip155:1", "tx_hash": "0xdeadbeef"}
                request = wallet.received[-1]["params"]["request"]
                assert request["method"] == "eth_sendTransaction"
                assert request["params"] == [
                    {"from": ADDRESS, "to": "0x" + "22" * 20, "value": "0x1", "data": "0x8456cb59"}
                ]

                wallet.answers["wc_sessionRequest"] = {"code": 5000, "message": "User rejected."}
                rejected = await mcp.call_tool("sign_message", {"message": "no"})
                assert rejected.is_error
                assert "5000" in rejected.content[0].text

                checked = await _call(mcp, "session_status", check_reachable=True)
                assert checked["sessions"][0]["reachable"] is True
            finally:
                await wallet.close()
