"""Tests for the MCP server example (examples/mcp_server), against the fake relay and wallet."""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("mcp")
pytest.importorskip("segno")
from mcp import Client  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from examples.mcp_server.server import (  # noqa: E402
    METADATA,
    SHOW_QR,
    SVG_BEGIN,
    SVG_END,
    create_server,
    make_qr,
    normalize_chain_id,
    parse_quantity,
    svg_inline_qr,
    with_eip712_domain,
)
from tests.fake_relay import run_fake_relay  # noqa: E402
from tests.fake_wallet import NOT_ANSWERED, FakeWallet  # noqa: E402
from walletconnect.sign import SignClient  # noqa: E402

TYPED = {
    "types": {"Mail": [{"name": "contents", "type": "string"}]},
    "primaryType": "Mail",
    "domain": {"name": "test", "version": "1", "chainId": 31337},
    "message": {"contents": "hi"},
}
ADDRESS = "0x" + "11" * 20
# A typical pairing URI (~190 characters).
URI = "wc:" + "a" * 64 + "@2?relay-protocol=irn&symKey=" + "b" * 64 + "&expiryTimestamp=1793671855"


class RecordingClient(SignClient):
    """Remembers the pairing URIs, which the server never returns."""

    uris: list[str] = []

    async def connect(self, *args, **kwargs):
        uri, task = await super().connect(*args, **kwargs)
        self.uris.append(uri)
        return uri, task


def _factory(url: str):
    RecordingClient.uris = []
    return lambda: RecordingClient(
        project_id="test", relay_url=url, metadata=METADATA, connect_timeout=10, request_timeout=1
    )


async def _call(mcp: Client, tool: str, **arguments) -> dict:
    result = await mcp.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content  # type: ignore[no-any-return]


async def _error(mcp: Client, tool: str, **arguments) -> str:
    result = await mcp.call_tool(tool, arguments)
    assert result.is_error, result.structured_content
    text: str = result.content[0].text  # type: ignore[union-attr]
    return text.removeprefix(f"Error executing tool {tool}: ")  # added by MCPServer


async def _pair(mcp: Client, url: str) -> FakeWallet:
    pair = await _call(mcp, "pair")
    wallet = await FakeWallet(url, RecordingClient.uris[-1]).start()
    status = await _call(mcp, "wait_for_pairing", pairing_topic=pair["pairing_topic"], timeout_s=5)
    assert status["status"] == "settled", status
    return wallet


def _rasterize(svg: str) -> list[list[bool]]:
    """Paint the single stroked path of an inline SVG QR back into a module grid."""
    match = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert match is not None
    (size,) = {int(n) for n in match.groups()}
    (d,) = re.findall(r'<path stroke="#000" d="([^"]*)"/>', svg)
    grid = [[False] * size for _ in range(size)]
    x = y = 0.0
    for op, a, b in re.findall(r"([Mmh])(-?[\d.]+)(?: (-?[\d.]+))?", d):
        if op == "h":
            for col in range(int(x), int(x + float(a))):
                grid[int(y)][col] = True
            x += float(a)
        else:
            x, y = (float(a), float(b)) if op == "M" else (x + float(a), y + float(b))
    return grid


def _matrix(uri: str, border: int = 4) -> list[list[bool]]:
    rows = [[bool(v) for v in row] for row in make_qr(uri).matrix]
    pad = [[False] * (len(rows) + 2 * border) for _ in range(border)]
    return pad + [[False] * border + row + [False] * border for row in rows] + pad


def test_svg_inline_qr():
    qr = make_qr(URI)
    assert (qr.error, qr.version, qr.is_micro) == ("L", 8, False)  # level L: fewest modules
    svg = svg_inline_qr(URI)
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 57 57" ')
    assert svg.count("<path") == 1  # every dark module in ONE path
    assert 'width="100%"' in svg
    assert 'shape-rendering="crispEdges"' in svg
    assert '<rect width="57" height="57" fill="#fff"/>' in svg  # white background
    assert "wc:" not in svg
    assert len(svg.encode()) < 4500
    assert _rasterize(svg) == _matrix(URI)  # same modules, 4-module white quiet zone


async def test_mcp_example_end_to_end():
    async with run_fake_relay() as (_fake, url), Client(create_server(_factory(url))) as mcp:
        tools = {tool.name for tool in (await mcp.list_tools()).tools}
        assert tools == {
            "pair",
            "wait_for_pairing",
            "session_status",
            "disconnect",
            "sign_message",
            "sign_typed_data",
            "send_transaction",
        }
        assert SHOW_QR in " ".join((mcp.instructions or "").split())
        assert await _call(mcp, "session_status") == {"sessions": []}
        assert (await _error(mcp, "sign_message", message="hi")).startswith("session_ended:")

        # pair: the SVG, between markers, as plain text; never the wc: URI.
        raw = await mcp.call_tool("pair", {})
        pair = raw.structured_content
        (uri,) = RecordingClient.uris
        [text] = raw.content
        body = text.text  # type: ignore[union-attr]
        assert f"{SVG_BEGIN}\n{pair['qr_svg']}\n{SVG_END}" in body
        assert pair["next"] == SHOW_QR
        for dumped in (body, json.dumps(pair)):
            assert uri not in dumped
            assert "symKey" not in dumped
        assert _rasterize(pair["qr_svg"]) == _matrix(uri)
        pending = await _call(
            mcp, "wait_for_pairing", pairing_topic=pair["pairing_topic"], timeout_s=0.1
        )
        assert pending["status"] == "pending"

        wallet = await FakeWallet(url, uri).start()  # "scans" the QR
        try:
            status = await _call(
                mcp, "wait_for_pairing", pairing_topic=pair["pairing_topic"], timeout_s=5
            )
            assert status["status"] == "settled", status
            assert status["session"]["peer_name"] == "FakeWallet"
            [session] = (await _call(mcp, "session_status"))["sessions"]
            assert session["chains"] == ["eip155:1"]
            assert session["expiry_iso"].endswith("Z")

            signed = await _call(mcp, "sign_message", message="hello", chain_id="1")
            assert signed == {"address": ADDRESS, "chain_id": "eip155:1", "signature": "0xdeadbeef"}
            assert wallet.received[-1]["params"]["request"]["method"] == "personal_sign"
            unapproved = await _error(mcp, "sign_message", message="x", chain_id="eip155:137")
            assert unapproved.startswith("unapproved_chain:")

            signed = await _call(mcp, "sign_typed_data", typed_data=TYPED)
            assert signed["added_eip712_domain"] is True
            request = wallet.received[-1]["params"]["request"]
            assert request["method"] == "eth_signTypedData_v4"
            assert "EIP712Domain" in json.loads(request["params"][1])["types"]

            big = str(10**30 + 1)  # beyond float precision
            sent = await _call(mcp, "send_transaction", to=ADDRESS, value_wei=big, data="0x01")
            assert sent == {"from": ADDRESS, "chain_id": "eip155:1", "tx_hash": "0xdeadbeef"}
            tx = {"from": ADDRESS, "to": ADDRESS, "value": hex(10**30 + 1), "data": "0x01"}
            assert wallet.received[-1]["params"]["request"]["params"] == [tx]

            wallet.answers["wc_sessionRequest"] = {"code": 5000, "message": "User rejected."}
            assert (await _error(mcp, "sign_message", message="no")).startswith("rejected:")
            wallet.answers["wc_sessionRequest"] = NOT_ANSWERED
            assert (await _error(mcp, "sign_message", message="slow")).startswith("timeout:")

            done = await _call(mcp, "disconnect")
            assert done == {"disconnected": wallet.topic, "sessions_left": 0}
            assert wallet.received[-1]["method"] == "wc_sessionDelete"
        finally:
            await wallet.close()


async def test_mcp_example_text_qr_and_unknown_pairing():
    async with run_fake_relay() as (_fake, url), Client(create_server(_factory(url))) as mcp:
        pair = await _call(mcp, "pair", qr="text")
        lines = pair["qr_text"].splitlines()
        assert set("".join(lines)) <= set("█▀▄ ")  # half blocks: two module rows per line
        assert "qr_svg" not in pair
        assert "symKey" not in json.dumps(pair)
        unknown = await _call(mcp, "wait_for_pairing", pairing_topic="00" * 32, timeout_s=0.1)
        assert unknown["status"] == "unknown"


async def test_mcp_example_several_sessions_need_a_topic():
    async with run_fake_relay() as (_fake, url), Client(create_server(_factory(url))) as mcp:
        first = await _pair(mcp, url)
        second = await _pair(mcp, url)
        try:
            message = await _error(mcp, "sign_message", message="which?")
            assert message.startswith("ambiguous_session:")
            assert first.topic in message
            assert second.topic in message
            signed = await _call(mcp, "sign_message", message="this one", topic=second.topic)
            assert signed["signature"] == "0xdeadbeef"
            gone = await _error(mcp, "sign_message", message="x", topic="ab" * 32)
            assert gone.startswith("session_ended:")
        finally:
            await first.close()
            await second.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("eip155:1", "eip155:1"), ("1", "eip155:1"), (137, "eip155:137"), ("0x89", "eip155:137")],
)
def test_normalize_chain_id(raw, expected):
    assert normalize_chain_id(raw) == expected


@pytest.mark.parametrize("raw", ["solana:1", "eip155:", "eip155:x", "0", -1, "abc", True])
def test_normalize_chain_id_rejects(raw):
    with pytest.raises(ToolError, match="invalid_chain"):
        normalize_chain_id(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, 0), ("0", 0), ("1000000000000000000", 10**18), ("0xde0b", 0xDE0B), (str(2**200), 2**200)],
)
def test_parse_quantity(raw, expected):
    assert parse_quantity(raw, "value_wei") == expected


@pytest.mark.parametrize("raw", ["1.5", "-1", -1, "1e18", "", "0xzz", True, str(2**256)])
def test_parse_quantity_rejects(raw):
    with pytest.raises(ToolError, match="invalid_argument"):
        parse_quantity(raw, "value_wei")


def test_eip712_domain_passthrough_and_auto_add():
    given = {**TYPED, "types": {**TYPED["types"], "EIP712Domain": []}}
    assert with_eip712_domain(given) == (given, False)
    added, was_added = with_eip712_domain(TYPED)
    assert was_added
    assert "EIP712Domain" not in TYPED["types"]
    assert [f["name"] for f in added["types"]["EIP712Domain"]] == ["name", "version", "chainId"]
    with pytest.raises(ToolError, match="non-standard"):
        with_eip712_domain({**TYPED, "domain": {"name": "x", "extra": 1}})
    with pytest.raises(ToolError, match="missing"):
        with_eip712_domain({"types": {}, "primaryType": "Mail", "message": {}})
