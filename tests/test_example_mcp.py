"""Smoke tests for examples/mcp_server: drive the tools in-process against the fake relay."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import stat
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("segno")
from mcp import Client  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from examples.mcp_server import server as server_module  # noqa: E402
from examples.mcp_server.server import (  # noqa: E402
    METADATA,
    OPEN_QR_ENV,
    QR_DIR_ENV,
    SVG_BEGIN,
    SVG_END,
    WALLET_DEEP_LINKS,
    _make_qr,
    _open_in_viewer,
    _open_qr_decision,
    _viewer_command,
    create_server,
    normalize_chain_id,
    parse_quantity,
    svg_inline_qr,
    wallet_deep_link,
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
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(autouse=True)
def _no_viewer(monkeypatch):
    """Never pop up a real image viewer from the test suite."""
    monkeypatch.setenv(OPEN_QR_ENV, "0")


class FakeOpener:
    """Stands in for asyncio.create_subprocess_exec: records the call, exits with ``code``."""

    def __init__(self, code: int | None = 0, error: OSError | None = None):
        self.code, self.error = code, error
        self.calls: list[tuple[tuple, dict]] = []
        self.waited = asyncio.Event()

    async def __call__(self, *cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.error:
            raise self.error
        return self

    async def wait(self) -> int:
        if self.code is None:  # a viewer that keeps running
            await asyncio.sleep(0.2)
            self.waited.set()
            return 0
        return self.code


async def _call(mcp: Client, tool: str, **arguments) -> dict:
    result = await mcp.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content  # type: ignore[no-any-return]


async def _error(mcp: Client, tool: str, **arguments) -> str:
    result = await mcp.call_tool(tool, arguments)
    assert result.is_error, result.structured_content
    text: str = result.content[0].text  # type: ignore[union-attr]
    prefix = f"Error executing tool {tool}: "  # added by MCPServer
    return text.removeprefix(prefix)


def _factory(url: str):
    def factory() -> SignClient:
        return SignClient(
            project_id="test",
            relay_url=url,
            metadata=METADATA,
            connect_timeout=10,
            request_timeout=10,
        )

    return factory


async def _pair(mcp: Client, url: str, **arguments) -> tuple[dict, FakeWallet]:
    arguments.setdefault("qr", "png")
    pair = await _call(mcp, "pair", include_uri=True, **arguments)
    wallet = await FakeWallet(url, pair["uri"]).start()
    status = await _call(mcp, "wait_for_pairing", pairing_topic=pair["pairing_topic"], timeout_s=5)
    assert status["status"] == "settled", status
    assert status["session_topic"] == wallet.topic
    return pair, wallet


async def test_mcp_example_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv(QR_DIR_ENV, str(tmp_path))
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
        assert await _call(mcp, "session_status") == {"sessions": []}
        assert (await _error(mcp, "sign_message", message="hi")).startswith("session_ended:")

        instructions = mcp.instructions or ""
        render = (
            "After pair, render the returned SVG exactly as given with your inline widget/artifact"
            " tool at a large size, tell the user to scan it with their wallet, then call"
            ' wait_for_pairing. If you cannot render widgets, call pair again with qr="text" or'
            ' qr="png".'
        )
        assert render in " ".join(instructions.split())
        assert "Never paste the wc: URI" in instructions
        assert "check qr_opened" in instructions
        pair_tool = next(t for t in (await mcp.list_tools()).tools if t.name == "pair")
        description = " ".join((pair_tool.description or "").split())
        assert render in description
        assert "if qr_opened is true, tell the user the QR code opened in a window" in description

        quiet = await mcp.call_tool("pair", {"qr": "png"})  # no URI anywhere in the result
        assert "symKey" not in quiet.content[0].text
        assert "uri" not in quiet.structured_content

        raw = await mcp.call_tool("pair", {"qr": "png", "include_uri": True})
        assert not raw.is_error
        pair = raw.structured_content
        assert pair["qr_format"] == "png"
        assert pair["uri"].startswith("wc:")
        assert 512 <= pair["qr_width_px"] <= 700  # big enough to scan inline
        assert pair["seconds_remaining"] > 0
        qr_file = Path(pair["qr_path"])
        assert qr_file.parent == tmp_path
        assert qr_file.read_bytes().startswith(PNG_MAGIC)
        assert stat.S_IMODE(os.stat(qr_file).st_mode) == 0o600
        text, image, link = raw.content
        assert text.type == "text"
        assert f"Show this QR code to the user so they can scan it: {qr_file}" in text.text
        assert qr_file.as_uri() in text.text
        assert f"pairing_topic: {pair['pairing_topic']}" in text.text
        # Short: no inline QR data (paths and the opt-in URI vary in length).
        short = text.text.replace(pair["uri"], "").replace(qr_file.as_uri(), "")
        assert len(short.replace(str(qr_file), "")) < 800
        assert image.type == "image"
        assert image.mime_type == "image/png"
        assert base64.b64decode(image.data) == qr_file.read_bytes()
        assert "<svg" not in json.dumps(pair)
        assert link.type == "resource_link"
        assert link.uri == pair["qr_resource"]
        assert pair["qr_resource"] == f"walletconnect://pairing/{pair['pairing_topic']}/qr.png"
        resource = await mcp.read_resource(pair["qr_resource"])
        assert base64.b64decode(resource.contents[0].blob) == qr_file.read_bytes()

        pending = await _call(mcp, "session_status", pairing_topic=pair["pairing_topic"])
        assert pending["pairing"]["status"] == "pending"
        assert 0 < pending["pairing"]["seconds_remaining"] <= 300

        wallet = await FakeWallet(url, pair["uri"]).start()
        try:
            status = await _call(
                mcp, "wait_for_pairing", pairing_topic=pair["pairing_topic"], timeout_s=5
            )
            assert status["status"] == "settled", status
            assert status["session"]["peer_name"] == "FakeWallet"
            assert not qr_file.exists()  # the secret-bearing QR is cleaned up

            [session] = (await _call(mcp, "session_status", check_reachable=True))["sessions"]
            assert session["reachable"] is True
            assert session["chains"] == ["eip155:1"]
            assert session["expiry_iso"].endswith("Z")
            assert 0 < session["expires_in_s"] <= 3600
            assert isinstance(session["expiry"], int)

            signed = await _call(mcp, "sign_message", message="hello", chain_id="1")
            assert signed == {
                "address": ADDRESS,
                "chain_id": "eip155:1",
                "signature": "0xdeadbeef",
            }
            request = wallet.received[-1]["params"]
            assert request["request"]["method"] == "personal_sign"
            assert request["chainId"] == "eip155:1"

            unapproved = await _error(mcp, "sign_message", message="x", chain_id="eip155:137")
            assert unapproved.startswith("unapproved_chain:")
            assert "eip155:1" in unapproved
            bad_chain = await _error(mcp, "sign_message", message="x", chain_id="mainnet")
            assert bad_chain.startswith("invalid_chain:")
            bad_account = await _error(mcp, "sign_message", message="x", address="0x" + "33" * 20)
            assert bad_account.startswith("invalid_account:")

            signed = await _call(mcp, "sign_typed_data", typed_data=TYPED)
            assert signed["signature"] == "0xdeadbeef"
            assert signed["added_eip712_domain"] is True
            assert "eip155:31337" in signed["warnings"][0]
            request = wallet.received[-1]["params"]["request"]
            assert request["method"] == "eth_signTypedData_v4"
            sent_typed = json.loads(request["params"][1])
            assert sent_typed["types"]["EIP712Domain"] == [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ]

            big = str(10**30 + 1)  # beyond float precision
            sent = await _call(
                mcp, "send_transaction", to="0x" + "22" * 20, value_wei=big, data="0x8456cb59"
            )
            assert sent == {"from": ADDRESS, "chain_id": "eip155:1", "tx_hash": "0xdeadbeef"}
            request = wallet.received[-1]["params"]["request"]
            assert request["method"] == "eth_sendTransaction"
            assert request["params"] == [
                {
                    "from": ADDRESS,
                    "to": "0x" + "22" * 20,
                    "value": hex(10**30 + 1),
                    "data": "0x8456cb59",
                }
            ]
            bad_value = await _error(mcp, "send_transaction", to=ADDRESS, value_wei="1.5")
            assert bad_value.startswith("invalid_argument:")

            wallet.answers["wc_sessionRequest"] = {"code": 5000, "message": "User rejected."}
            assert (await _error(mcp, "sign_message", message="no")).startswith("rejected:")

            wallet.answers["wc_sessionRequest"] = NOT_ANSWERED
            timeout = await _error(mcp, "sign_message", message="slow", timeout_s=0.3)
            assert timeout.startswith("timeout:")

            gone = await _error(mcp, "sign_message", message="x", topic="ab" * 32)
            assert gone.startswith("session_ended:")

            done = await _call(mcp, "disconnect")
            assert done == {
                "disconnected": wallet.topic,
                "peer_name": "FakeWallet",
                "sessions_left": 0,
            }
            assert wallet.received[-1]["method"] == "wc_sessionDelete"
            assert (await _call(mcp, "session_status"))["sessions"] == []
        finally:
            await wallet.close()


async def test_mcp_example_several_sessions_need_a_topic(tmp_path, monkeypatch):
    monkeypatch.setenv(QR_DIR_ENV, str(tmp_path))
    async with run_fake_relay() as (_fake, url), Client(create_server(_factory(url))) as mcp:
        _, first = await _pair(mcp, url, qr="none")
        _, second = await _pair(mcp, url, qr="svg", include_image=False)
        try:
            message = await _error(mcp, "sign_message", message="which?")
            assert message.startswith("ambiguous_session:")
            assert first.topic in message
            assert second.topic in message
            assert (await _error(mcp, "disconnect")).startswith("ambiguous_session:")
            signed = await _call(mcp, "sign_message", message="this one", topic=second.topic)
            assert signed["signature"] == "0xdeadbeef"
            assert second.received[-1]["params"]["request"]["method"] == "personal_sign"
        finally:
            await first.close()
            await second.close()


async def test_mcp_example_pair_options(tmp_path, monkeypatch):
    monkeypatch.delenv(QR_DIR_ENV, raising=False)
    async with run_fake_relay() as (_fake, url):
        async with Client(create_server(_factory(url))) as mcp:
            target = tmp_path / "nested" / "qr.svg"
            pair = await _call(mcp, "pair", qr="svg", qr_path=str(target), include_uri=False)
            assert "uri" not in pair
            assert "symKey" not in json.dumps(pair)
            assert Path(pair["qr_path"]) == target.resolve()
            assert target.read_text().startswith("<svg")

            text = await _call(mcp, "pair", qr="text")
            assert "qr_path" not in text
            lines = text["qr"].splitlines()
            assert set("".join(lines)) <= set("█▀▄ ")  # half blocks: two module rows per line
            assert len(lines) <= 32
            assert max(map(len, lines)) <= 64

            small = await _call(mcp, "pair", qr="png", qr_scale=4)
            big = await _call(mcp, "pair", qr="png", qr_scale=12)
            assert small["qr_width_px"] * 3 == big["qr_width_px"]
            bad_scale = await _error(mcp, "pair", qr_scale=0)
            assert bad_scale.startswith("invalid_argument:")

            default = await _call(mcp, "pair", qr="png", include_image=False)
            assert Path(default["qr_path"]).exists()  # system temp dir
            raw = await mcp.call_tool("pair", {"qr": "png", "include_image": False})
            assert [c.type for c in raw.content] == ["text", "resource_link"]

            pending = await _call(
                mcp, "wait_for_pairing", pairing_topic=pair["pairing_topic"], timeout_s=0.1
            )
            assert pending["status"] == "pending"
            assert pending["seconds_remaining"] > 0
            unknown = await _call(mcp, "wait_for_pairing", pairing_topic="00" * 32, timeout_s=0.1)
            assert unknown["status"] == "unknown"
            nothing = await _error(mcp, "pair", qr="none", include_uri=False)
            assert nothing.startswith("invalid_argument:")
        # shutdown removes QR files of pairings that never settled
        assert not target.exists()
        assert not Path(default["qr_path"]).exists()


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
    given = {
        **TYPED,
        "types": {**TYPED["types"], "EIP712Domain": [{"name": "name", "type": "string"}]},
    }
    assert with_eip712_domain(given) == (given, False)

    added, was_added = with_eip712_domain(TYPED)
    assert was_added
    assert "EIP712Domain" not in TYPED["types"]
    assert [f["name"] for f in added["types"]["EIP712Domain"]] == ["name", "version", "chainId"]

    with pytest.raises(ToolError, match="non-standard"):
        with_eip712_domain({**TYPED, "domain": {"name": "x", "extra": 1}})
    with pytest.raises(ToolError, match="missing 'domain'"):
        with_eip712_domain({"types": {}, "primaryType": "Mail", "message": {}})


@pytest.mark.parametrize(
    ("arg", "env", "platform", "display", "expected"),
    [
        (True, "0", "linux", None, True),
        (False, "1", "darwin", ":0", False),
        (None, "1", "linux", None, True),
        (None, "yes", "linux", None, True),
        (None, "0", "darwin", ":0", False),
        (None, "", "darwin", None, True),
        (None, "", "win32", None, True),
        (None, "", "linux", ":0", True),
        (None, "", "linux", None, False),
    ],
)
def test_open_qr_decision(monkeypatch, arg, env, platform, display, expected):
    monkeypatch.setenv(OPEN_QR_ENV, env)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if display:
        monkeypatch.setenv("DISPLAY", display)
    else:
        monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    opened, reason = _open_qr_decision(arg)
    assert opened is expected
    assert reason


def test_open_qr_decision_wayland(monkeypatch):
    monkeypatch.setenv(OPEN_QR_ENV, "")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(sys, "platform", "linux")
    assert _open_qr_decision(None)[0] is True


@pytest.mark.parametrize(("platform", "opener"), [("linux", "xdg-open"), ("darwin", "open")])
async def test_open_in_viewer_is_silent_and_non_blocking(monkeypatch, tmp_path, platform, opener):
    monkeypatch.setattr(sys, "platform", platform)
    fake = FakeOpener()
    monkeypatch.setattr(server_module.asyncio, "create_subprocess_exec", fake)
    path = tmp_path / "qr.png"
    assert await _open_in_viewer(path, lambda _aw: None) == (True, f"opened with {opener}")
    ((cmd, kwargs),) = fake.calls
    assert list(cmd) == [opener, str(path)]
    for stream in ("stdin", "stdout", "stderr"):  # stdout is the MCP channel
        assert kwargs[stream] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


async def test_open_in_viewer_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    path = tmp_path / "qr.png"
    monkeypatch.setattr(server_module.asyncio, "create_subprocess_exec", FakeOpener(code=3))
    assert await _open_in_viewer(path, lambda _aw: None) == (
        False,
        "xdg-open exited with status 3",
    )
    missing = FakeOpener(error=FileNotFoundError("xdg-open"))
    monkeypatch.setattr(server_module.asyncio, "create_subprocess_exec", missing)
    opened, reason = await _open_in_viewer(path, lambda _aw: None)
    assert not opened
    assert reason.startswith("could not run xdg-open")


async def test_open_in_viewer_reaps_a_slow_opener(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(server_module, "VIEWER_CHECK_S", 0.01)
    slow = FakeOpener(code=None)
    monkeypatch.setattr(server_module.asyncio, "create_subprocess_exec", slow)
    reaped = []
    opened, _ = await _open_in_viewer(tmp_path / "qr.png", reaped.append)
    assert opened
    assert len(reaped) == 1
    await reaped[0]
    assert slow.waited.is_set()


async def test_mcp_example_pair_opens_the_qr(tmp_path, monkeypatch):
    monkeypatch.setenv(QR_DIR_ENV, str(tmp_path))
    fake = FakeOpener()
    monkeypatch.setattr(server_module.asyncio, "create_subprocess_exec", fake)
    async with run_fake_relay() as (_fake, url), Client(create_server(_factory(url))) as mcp:
        raw = await mcp.call_tool("pair", {"qr": "png", "open_qr": True, "include_uri": True})
        pair = raw.structured_content
        assert pair["qr_opened"] is True
        ((cmd, _),) = fake.calls
        assert list(cmd) == _viewer_command(Path(pair["qr_path"]))
        text = raw.content[0].text  # type: ignore[union-attr]
        assert "The QR code was opened in an image viewer window" in text
        assert "Show this QR code" not in text
        assert "opened in your image viewer" in pair["next"]
        # The file outlives the call so the viewer can load it; it goes once the pairing settles.
        qr_file = Path(pair["qr_path"])
        assert qr_file.exists()
        wallet = await FakeWallet(url, pair["uri"]).start()
        await _call(mcp, "wait_for_pairing", pairing_topic=pair["pairing_topic"], timeout_s=5)
        assert not qr_file.exists()
        await wallet.close()

        # Default with WALLETCONNECT_OPEN_QR=0 (see the autouse fixture): not opened, path shown.
        raw = await mcp.call_tool("pair", {"qr": "png"})
        pair = raw.structured_content
        assert pair["qr_opened"] is False
        assert pair["qr_open_reason"] == f"{OPEN_QR_ENV}=0"
        assert "Show this QR code to the user" in raw.content[0].text  # type: ignore[union-attr]
        assert len(fake.calls) == 1

        # A failing opener falls back to showing the path.
        monkeypatch.setattr(server_module.asyncio, "create_subprocess_exec", FakeOpener(code=1))
        raw = await mcp.call_tool("pair", {"qr": "png", "open_qr": True})
        assert raw.structured_content["qr_opened"] is False
        assert "exited with status 1" in raw.content[0].text  # type: ignore[union-attr]

        text_pair = await _call(mcp, "pair", qr="text", open_qr=True)
        assert text_pair["qr_opened"] is False


URI = (
    "wc:" + "7f" * 32 + "@2?relay-protocol=irn&symKey=" + "a1" * 32 + "&expiryTimestamp=1794274459"
)


def _rasterize(svg: str) -> tuple[int, list[list[bool]]]:
    """Paint the single stroked path of an inline SVG QR back into a module grid."""
    match = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert match is not None
    (size,) = {int(n) for n in match.groups()}
    (d,) = re.findall(r'<path stroke="#000" d="([^"]*)"/>', svg)
    grid = [[False] * size for _ in range(size)]
    x = y = 0.0
    for op, a, b in re.findall(r"([Mmh])(-?[\d.]+)(?: (-?[\d.]+))?", d):
        if op == "M":
            x, y = float(a), float(b)
        elif op == "m":
            x, y = x + float(a), y + float(b)
        else:
            for col in range(int(x), int(x + float(a))):
                grid[int(y)][col] = True
            x += float(a)
    return size, grid


def _matrix(uri: str, border: int = 4) -> list[list[bool]]:
    import segno

    qr = segno.make(uri, error="l", boost_error=False, micro=False)
    rows = [[bool(v) for v in row] for row in qr.matrix]
    width = len(rows) + 2 * border
    pad = [[False] * width for _ in range(border)]
    return pad + [[False] * border + row + [False] * border for row in rows] + pad


def test_pairing_qr_is_level_l():
    # Error level L exactly (no boost to M/Q/H) gives the fewest modules: version 8 (49
    # modules, 57 with the quiet zone) for a typical ~190-char pairing URI, versus 10 at M.
    uri = (
        "wc:"
        + "a" * 64
        + "@2?relay-protocol=irn&symKey="
        + "b" * 64
        + "&expiryTimestamp=1793671855"
    )
    qr = _make_qr(uri)
    assert qr.error == "L"
    assert not qr.is_micro
    assert qr.version == 8
    svg, size = svg_inline_qr(uri)
    assert size == 57
    assert len(svg.encode()) < 4500


def test_svg_inline_qr_shape():
    svg, size = svg_inline_qr(URI)
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ')
    assert not svg.startswith("<?xml")
    assert svg.count("<path") == 1  # every dark module in ONE path
    assert f'viewBox="0 0 {size} {size}"' in svg  # square
    assert 'width="100%"' in svg
    assert "height=" not in svg.split("<rect")[0]  # no fixed size on the <svg>
    assert 'shape-rendering="crispEdges"' in svg
    assert f'<rect width="{size}" height="{size}" fill="#fff"/>' in svg  # white background
    assert "<title" not in svg
    assert "<desc" not in svg
    assert "wc:" not in svg
    assert "symKey" not in svg
    assert len(svg.encode()) < 6000
    raster_size, grid = _rasterize(svg)
    assert raster_size == size
    assert grid == _matrix(URI)  # same modules, 4-module white quiet zone


async def test_mcp_example_pair_svg_inline_is_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv(QR_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(OPEN_QR_ENV)  # svg_inline must not auto-open without being asked
    monkeypatch.setenv("DISPLAY", ":0")
    opener = FakeOpener()
    monkeypatch.setattr(server_module.asyncio, "create_subprocess_exec", opener)
    uris: list[str] = []

    class Recording(SignClient):
        async def connect(self, *args, **kwargs):
            uri, task = await super().connect(*args, **kwargs)
            uris.append(uri)
            return uri, task

    def factory() -> SignClient:
        return Recording(project_id="test", relay_url=url, metadata=METADATA, connect_timeout=10)

    async with run_fake_relay() as (_fake, url), Client(create_server(factory)) as mcp:
        raw = await mcp.call_tool("pair", {})
        assert not raw.is_error, raw.content
        pair = raw.structured_content
        (uri,) = uris
        assert pair["qr_format"] == "svg_inline"
        assert pair["qr_opened"] is False
        assert "svg_inline" in pair["qr_open_reason"]
        assert opener.calls == []
        assert "qr_path" not in pair
        assert not list(tmp_path.iterdir())  # no file
        [text] = raw.content  # no image, no resource link: just the text
        body = text.text  # type: ignore[union-attr]
        # The URI (and its secret) never appears as text anywhere in the result.
        for dumped in (body, json.dumps(pair)):
            assert uri not in dumped
            assert "symKey" not in dumped
            assert uri.split("symKey=")[1].split("&")[0] not in dumped
        assert "uri" not in pair
        block = body.split(SVG_BEGIN + "\n")[1].split("\n" + SVG_END)[0]
        assert block == pair["qr_svg"]
        assert "wc:" not in block
        assert pair["qr_svg_bytes"] == len(block.encode()) < 6000
        assert f"({pair['qr_svg_bytes']} bytes)" in body
        _, grid = _rasterize(block)
        assert grid == _matrix(uri)
        assert pair["qr_modules"] == len(grid)
        assert "render the returned SVG exactly as given" in pair["next"]

        # The rendered QR pairs.
        wallet = await FakeWallet(url, uri).start()
        try:
            status = await _call(
                mcp, "wait_for_pairing", pairing_topic=pair["pairing_topic"], timeout_s=5
            )
            assert status["status"] == "settled"
        finally:
            await wallet.close()

        refused = await _error(mcp, "pair", include_uri=True)
        assert refused.startswith("invalid_argument:")
        assert len(uris) == 1  # refused before starting a pairing

        # Asking explicitly also opens a PNG in the viewer.
        raw = await mcp.call_tool("pair", {"open_qr": True})
        pair = raw.structured_content
        assert pair["qr_opened"] is True
        assert Path(pair["qr_path"]).read_bytes().startswith(PNG_MAGIC)
        assert "qr_svg" in pair
        assert len(opener.calls) == 1
        monkeypatch.setenv(OPEN_QR_ENV, "1")
        assert (await _call(mcp, "pair"))["qr_opened"] is True


PAIRING_URI = (
    "wc:7f6e1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f@2"
    "?relay-protocol=irn&symKey=" + "ab" * 32 + "&expiryTimestamp=1793671855"
)


@pytest.mark.parametrize(
    ("wallet", "prefix"),
    [
        ("metamask", "https://metamask.app.link/wc?uri="),
        ("rainbow", "https://rnbwapp.com/wc?uri="),
        ("trust", "https://link.trustwallet.com/wc?uri="),
        ("uniswap", "https://uniswap.org/app/wc?uri="),
        ("zerion", "zerion://wc?uri="),
        (" MetaMask ", "https://metamask.app.link/wc?uri="),
    ],
)
def test_wallet_deep_link(wallet, prefix):
    name, link = wallet_deep_link(PAIRING_URI, wallet)
    assert name
    assert link.startswith(prefix)
    encoded = link.removeprefix(prefix)
    # URL-encoded like encodeURIComponent: no raw separators survive...
    assert not set(":@?&=/") & set(encoded)
    # ...and it decodes back to exactly the pairing URI.
    query = urllib.parse.urlsplit(link).query
    assert urllib.parse.parse_qs(query, strict_parsing=True) == {"uri": [PAIRING_URI]}


def test_wallet_deep_link_table_prefers_https():
    for key, (_name, base) in WALLET_DEEP_LINKS.items():
        assert base.endswith("wc?uri=")
        assert base.startswith("https://") or key == "zerion"  # see the comment in the table


@pytest.mark.parametrize("wallet", ["generic", "universal", "Generic"])
def test_wallet_deep_link_generic_is_the_wc_uri(wallet):
    _name, link = wallet_deep_link(PAIRING_URI, wallet)
    assert link == PAIRING_URI


def test_wallet_deep_link_rejects_unknown_wallets():
    with pytest.raises(ValueError, match="metamask, rainbow, trust, uniswap, zerion"):
        wallet_deep_link(PAIRING_URI, "phantom")


async def test_mcp_example_pair_deep_link(tmp_path, monkeypatch):
    monkeypatch.setenv(QR_DIR_ENV, str(tmp_path))
    uris: list[str] = []

    class Recording(SignClient):
        async def connect(self, *args, **kwargs):
            uri, task = await super().connect(*args, **kwargs)
            uris.append(uri)
            return uri, task

    def factory() -> SignClient:
        return Recording(project_id="test", relay_url=url, metadata=METADATA, connect_timeout=10)

    async with run_fake_relay() as (_fake, url), Client(create_server(factory)) as mcp:
        # Off by default.
        pair = await _call(mcp, "pair")
        assert not {"deep_link", "deep_link_wallet", "deep_link_warning"} & set(pair)

        # Opt-in, alongside the default svg_inline QR.
        raw = await mcp.call_tool("pair", {"deep_link": "trust"})
        assert not raw.is_error, raw.content
        pair = raw.structured_content
        uri = uris[-1]
        assert pair["qr_format"] == "svg_inline"
        assert "qr_svg" in pair
        assert "uri" not in pair
        prefix = "https://link.trustwallet.com/wc?uri="
        assert pair["deep_link"] == prefix + urllib.parse.quote(uri, safe="")
        assert pair["deep_link_wallet"] == "Trust Wallet"
        assert "symKey" in pair["deep_link_warning"]
        body = raw.content[0].text  # type: ignore[union-attr]
        assert pair["deep_link"] in body
        assert "contains the pairing secret" in body
        _, grid = _rasterize(pair["qr_svg"])
        assert grid == _matrix(uri)  # the QR is unchanged

        # The link pairs.
        query = urllib.parse.urlsplit(pair["deep_link"]).query
        (decoded,) = urllib.parse.parse_qs(query)["uri"]
        assert decoded == uri
        wallet = await FakeWallet(url, decoded).start()
        try:
            status = await _call(
                mcp, "wait_for_pairing", pairing_topic=pair["pairing_topic"], timeout_s=5
            )
            assert status["status"] == "settled"
        finally:
            await wallet.close()

        # A deep link alone is enough for qr="none".
        pair = await _call(mcp, "pair", qr="none", deep_link="metamask")
        assert pair["deep_link"].startswith("https://metamask.app.link/wc?uri=wc%3A")

        count = len(uris)
        refused = await _error(mcp, "pair", deep_link="phantom")
        assert refused.startswith("invalid_argument:")
        assert "metamask" in refused
        assert len(uris) == count  # refused before starting a pairing


def test_instructions_offer_deep_links_only_when_needed():
    instructions = " ".join(server_module.INSTRUCTIONS.split())
    assert "deep link only if the user is on the same phone" in instructions
    for key in WALLET_DEEP_LINKS:
        assert key in instructions
