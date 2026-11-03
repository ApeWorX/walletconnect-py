"""
MCP server example: let an AI assistant pair a mobile wallet and ask it to sign.

Uses the official ``mcp`` Python SDK (``MCPServer``, formerly ``FastMCP``) over stdio. The
assistant gets these tools: ``pair``, ``wait_for_pairing``, ``session_status``, ``disconnect``,
``sign_message``, ``sign_typed_data`` and ``send_transaction``. Nothing is signed here: every
request is approved (or rejected) by the user in their wallet app.

Run it::

    export WALLETCONNECT_PROJECT_ID=your_32_char_id    # https://dashboard.walletconnect.com
    export WALLETCONNECT_STATE_FILE=./wc-mcp.json      # optional: survive restarts
    export WALLETCONNECT_QR_DIR=~/wc-qr                # optional: where pair writes QR images
    uv run --group examples python -m examples.mcp_server

This is an example. The production MCP integration is planned as a feature of the
``ape-walletconnect`` plugin, so any Ape-based MCP server can reuse the same tools.

.. warning:: The state file holds secret keys (relay identity + session keys). It is written
   with ``0600`` permissions; keep it out of version control and backups you don't trust.
   The pairing URI (and so the QR image) holds a short-lived secret too; QR files are written
   ``0600`` and deleted once the pairing settles or fails.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, ResourceLink, TextContent

from walletconnect import DEFAULT_RELAY_URL, ClientState, Metadata, PairingURI
from walletconnect.constants import PAIRING_PROPOSAL_EXPIRY
from walletconnect.exceptions import (
    NoActiveSessionError,
    RelayConnectionError,
    SdkError,
    SessionDeletedError,
    SessionExpiredError,
    UserRejectedError,
    UserRejectedRequestError,
    WalletConnectError,
    WalletConnectTimeoutError,
)
from walletconnect.sign import SignClient

logger = logging.getLogger("walletconnect.examples.mcp")

STATE_FILE_ENV = "WALLETCONNECT_STATE_FILE"
RELAY_URL_ENV = "WALLETCONNECT_RELAY_URL"
REQUEST_TIMEOUT_ENV = "WALLETCONNECT_REQUEST_TIMEOUT"
QR_DIR_ENV = "WALLETCONNECT_QR_DIR"
OPEN_QR_ENV = "WALLETCONNECT_OPEN_QR"
# How long to wait for xdg-open/open to report a failure before assuming the viewer opened.
VIEWER_CHECK_S = 2.0
DEFAULT_REQUEST_TIMEOUT = 300.0  # the user has to notice the request on their phone
DEFAULT_WAIT_TIMEOUT = 60.0
MAX_WAIT_TIMEOUT = float(PAIRING_PROPOSAL_EXPIRY)
QR_TARGET_PX = 600  # inline image width: big enough to scan off a laptop screen
QR_BORDER = 4  # quiet zone (modules) for PNG/SVG, as the QR spec recommends
QR_MIN_SCALE, QR_MAX_SCALE = 1, 20
TEXT_QR_BORDER = 2  # the smallest quiet zone that still scans reliably from a screen

# EIP-712 fields of the domain, in the order the standard lists them.
EIP712_DOMAIN_FIELDS = (
    ("name", "string"),
    ("version", "string"),
    ("chainId", "uint256"),
    ("verifyingContract", "address"),
    ("salt", "bytes32"),
)

METADATA = Metadata(
    name="WalletConnect Python MCP Demo",
    description="An AI assistant asks your wallet to sign; you approve on your phone",
    url="https://github.com/ApeWorX/walletconnect-py",
)

QR_RESOURCE_TEMPLATE = "walletconnect://pairing/{pairing_topic}/qr.png"

SVG_INLINE_GUIDANCE = (
    "After pair, render the returned SVG exactly as given with your inline widget/artifact tool"
    " at a large size, tell the user to scan it with their wallet, then call wait_for_pairing."
    ' If you cannot render widgets, call pair again with qr="text" or qr="png".'
)

SHOW_QR_GUIDANCE = (
    f'{SVG_INLINE_GUIDANCE} With qr="png" (or "svg"), check qr_opened: if it is true, the QR'
    " code is already open in an image viewer window on the user's screen, so tell them so;"
    " otherwise show the QR code prominently in your reply (render the image, or give them the"
    " file path or file:// link to open; the image inside the tool result may be collapsed in"
    " the client). Then call wait_for_pairing (or session_status) with the pairing_topic. Never"
    " paste the wc: URI unless the user asks for it: it contains a short-lived secret key."
)

SVG_BEGIN = "-----BEGIN QR SVG-----"
SVG_END = "-----END QR SVG-----"

INSTRUCTIONS = f"""\
Tools for a WalletConnect mobile wallet. Nothing is signed by this server: each signing tool sends
a request to the user's wallet app and waits until they approve or reject it there.
1. If session_status shows no sessions, call pair. By default (qr="svg_inline") it returns the QR
   code as SVG markup between {SVG_BEGIN} and {SVG_END}, and never the wc: URI. qr="png"
   writes a PNG file instead, opens it in the system image viewer on a desktop (qr_opened),
   returns its path and the image, and exposes it as the resource {QR_RESOURCE_TEMPLATE}.
2. {SHOW_QR_GUIDANCE}
3. wait_for_pairing returns as soon as the wallet approves, or "pending" with seconds_remaining;
   call it again while pending.
4. Use sign_message, sign_typed_data or send_transaction. Tell the user to check their phone.
   With several sessions, pass topic. Errors start with a category such as "rejected:",
   "timeout:" or "session_ended:".
"""


# ---------------------------------------------------------------- environment


def _state_path() -> Path | None:
    raw = os.environ.get(STATE_FILE_ENV)
    return Path(raw).expanduser() if raw else None


def default_client() -> SignClient:
    """A client configured from the environment (see the module docstring)."""
    path = _state_path()
    state = ClientState.load(path) if path is not None and path.exists() else None

    def save(snapshot: ClientState) -> None:
        if path is not None:
            snapshot.save(path)

    return SignClient(
        metadata=METADATA,
        relay_url=os.environ.get(RELAY_URL_ENV, DEFAULT_RELAY_URL),
        state=state,
        on_state_change=save,
        request_timeout=float(os.environ.get(REQUEST_TIMEOUT_ENV, DEFAULT_REQUEST_TIMEOUT)),
    )


# ---------------------------------------------------------------- helpers


def _iso(ts: float) -> str:
    """Unix seconds as ISO 8601 UTC, e.g. ``2026-11-09T12:00:00Z``."""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _session_info(client: SignClient, session: Any) -> dict[str, Any]:
    return {
        "topic": session.topic,
        "peer_name": session.peer_name,
        "accounts": session.accounts,
        "chains": sorted(_approved_chains(session)),
        "expiry": session.expiry,
        "expiry_iso": _iso(session.expiry),
        "expires_in_s": max(0, int(session.expiry - time.time())),
        "reachable": session.topic not in client.unreachable,
    }


def normalize_chain_id(chain_id: str | int) -> str:
    """
    Turn ``"eip155:1"``, ``"1"``, ``1`` or ``"0x1"`` into the CAIP-2 id ``"eip155:1"``.

    Raises :class:`ToolError` for anything else.
    """
    if isinstance(chain_id, bool):
        raise ToolError(f"invalid_chain: {chain_id!r} is not a chain id")
    if isinstance(chain_id, int):
        reference = chain_id
    else:
        raw = chain_id.strip()
        if ":" in raw:
            namespace, _, ref = raw.partition(":")
            if namespace != "eip155" or not ref.isdigit() or int(ref) <= 0:
                raise ToolError(
                    f"invalid_chain: {chain_id!r} is not an EVM CAIP-2 chain id like 'eip155:1'"
                )
            return f"eip155:{int(ref)}"
        try:
            reference = int(raw, 16) if raw.lower().startswith("0x") else int(raw, 10)
        except ValueError:
            raise ToolError(
                f"invalid_chain: {chain_id!r} is not a chain id; use 'eip155:1' or '1'"
            ) from None
    if reference <= 0:
        raise ToolError(f"invalid_chain: {chain_id!r} is not a positive chain id")
    return f"eip155:{reference}"


def parse_quantity(value: str | int, name: str) -> int:
    """
    Parse a non-negative integer given as a JSON int, a decimal string or a ``0x`` hex string.

    Strings avoid the precision loss some JSON clients have above 2**53.
    """
    if isinstance(value, bool):
        raise ToolError(f"invalid_argument: {name} must be an integer, not {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        raw = value.strip().replace("_", "")
        try:
            if raw.lower().startswith("0x"):
                number = int(raw, 16)
            elif raw.isdigit():
                number = int(raw, 10)
            else:
                raise ValueError(raw)
        except ValueError:
            raise ToolError(
                f"invalid_argument: {name}={value!r} must be a whole number of wei as an integer,"
                " a decimal string such as '1000000000000000000', or a hex string such as '0xde0b'"
            ) from None
    else:
        raise ToolError(f"invalid_argument: {name} must be an integer or string")
    if number < 0:
        raise ToolError(f"invalid_argument: {name} must not be negative")
    if number >= 2**256:
        raise ToolError(f"invalid_argument: {name} does not fit in uint256")
    return number


def _approved_chains(session: Any) -> set[str]:
    """CAIP-2 chains the session approved (``namespaces.eip155.chains``, plus account chains)."""
    ns = session.namespaces.get("eip155") or {}
    chains = {str(c) for c in ns.get("chains") or []}
    chains.update(":".join(a.split(":")[:2]) for a in ns.get("accounts") or [])
    return chains


def with_eip712_domain(typed_data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """
    Return ``typed_data`` with ``types.EIP712Domain`` filled in from ``domain`` when missing.

    An existing ``EIP712Domain`` is passed through untouched. Returns ``(typed_data, added)``.
    """
    for key in ("types", "primaryType", "domain", "message"):
        if key not in typed_data:
            raise ToolError(
                f"invalid_typed_data: missing {key!r}; expected an object with "
                "'types', 'primaryType', 'domain' and 'message'"
            )
    types = typed_data["types"]
    domain = typed_data["domain"]
    if not isinstance(types, dict) or not isinstance(domain, dict):
        raise ToolError("invalid_typed_data: 'types' and 'domain' must be objects")
    if "EIP712Domain" in types:
        return typed_data, False
    unknown = set(domain) - {name for name, _ in EIP712_DOMAIN_FIELDS}
    if unknown:
        raise ToolError(
            f"invalid_typed_data: domain has non-standard fields {sorted(unknown)}; "
            "pass types.EIP712Domain yourself to describe them"
        )
    result = copy.deepcopy(typed_data)
    result["types"] = {
        "EIP712Domain": [
            {"name": name, "type": kind} for name, kind in EIP712_DOMAIN_FIELDS if name in domain
        ],
        **result["types"],
    }
    return result, True


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _qr_target(qr_path: str | None, pairing_topic: str, suffix: str) -> Path:
    """``qr_path`` if given, else ``$WALLETCONNECT_QR_DIR/…``, else a new file in the temp dir."""
    if qr_path:
        return Path(qr_path).expanduser().resolve()
    name = f"wc-pair-{pairing_topic[:12]}{suffix}"
    if qr_dir := os.environ.get(QR_DIR_ENV):
        return (Path(qr_dir).expanduser() / name).resolve()
    fd, tmp = tempfile.mkstemp(prefix=f"wc-pair-{pairing_topic[:12]}-", suffix=suffix)
    os.close(fd)
    return Path(tmp)


def svg_inline_qr(uri: str) -> tuple[str, int]:
    """
    The QR as compact inline SVG markup, and its size in modules (quiet zone included).

    All dark modules are ONE ``<path>``: a 1-unit-wide stroke along each run of dark modules in
    a row (``h`` segments, relative moves), so a typical pairing URI comes to about 5 KB. A
    4-module white quiet zone and an explicit white ``<rect>`` keep it scannable in dark mode.
    The viewBox is square with no fixed size (``width="100%"``) and ``crispEdges``, so it scales
    up sharply. No XML declaration, title or description: the URI is only in the pattern.
    """
    import segno

    qr = segno.make(uri, error="m", micro=False)
    size = len(qr.matrix) + 2 * QR_BORDER
    parts: list[str] = []
    pen: tuple[int, int] | None = None  # where the last run ended (x, row)
    for row, line in enumerate(qr.matrix):
        y = row + QR_BORDER
        x = 0
        width = len(line)
        while x < width:
            if not line[x]:
                x += 1
                continue
            start = x
            while x < width and line[x]:
                x += 1
            sx = start + QR_BORDER
            if pen is None:
                parts.append(f"M{sx} {y}.5h{x - start}")
            else:
                parts.append(f"m{sx - pen[0]} {y - pen[1]}h{x - start}")
            pen = (x + QR_BORDER, y)
    path = "".join(parts)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="100%"'
        f' shape-rendering="crispEdges"><rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path stroke="#000" d="{path}"/></svg>'
    )
    return svg, size


def _render_qr_bytes(
    uri: str, kind: Literal["png", "svg"], scale: int | None = None
) -> tuple[bytes, int]:
    """
    Draw the QR as PNG or SVG (segno writes both itself: no Pillow needed).

    Without ``scale`` it is picked so the image is about ``QR_TARGET_PX`` wide, large enough
    to scan straight off the screen. Returns ``(data, width_px)``.
    """
    import segno

    qr = segno.make(uri, error="m", micro=False)
    if scale is None:
        modules = qr.symbol_size(scale=1, border=QR_BORDER)[0]
        scale = max(QR_MIN_SCALE, min(QR_MAX_SCALE, round(QR_TARGET_PX / modules)))
    buf = io.BytesIO()
    if kind == "png":
        qr.save(buf, kind="png", scale=scale, border=QR_BORDER)
    else:
        qr.save(buf, kind="svg", scale=scale, border=QR_BORDER, xmldecl=False, nl=False)
    return buf.getvalue(), int(qr.symbol_size(scale=scale, border=QR_BORDER)[0])


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _open_qr_decision(open_qr: bool | None, inline: bool = False) -> tuple[bool, str]:
    """
    Whether ``pair`` should open the QR in a viewer, and why (argument, env, then platform).
    With ``inline`` (``qr="svg_inline"``) the chat renders the QR, so only an explicit
    ``open_qr=true`` or ``WALLETCONNECT_OPEN_QR=1`` opens a viewer.
    """
    if open_qr is not None:
        return open_qr, f"open_qr={'true' if open_qr else 'false'}"
    raw = os.environ.get(OPEN_QR_ENV, "").strip().lower()
    if raw in _TRUE:
        return True, f"{OPEN_QR_ENV}={raw}"
    if raw in _FALSE:
        return False, f"{OPEN_QR_ENV}={raw}"
    if inline:
        return False, "qr='svg_inline' renders in the chat (open_qr=true also opens a viewer)"
    if sys.platform in ("darwin", "win32"):
        return True, f"desktop platform ({sys.platform})"
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True, "desktop session (DISPLAY/WAYLAND_DISPLAY set)"
    return False, "no desktop session detected (DISPLAY and WAYLAND_DISPLAY are unset)"


def _viewer_command(path: Path) -> list[str]:
    return ["open" if sys.platform == "darwin" else "xdg-open", str(path)]


async def _open_in_viewer(path: Path, reaper: Callable[[Awaitable[Any]], None]) -> tuple[bool, str]:
    """
    Open ``path`` in the default image viewer without blocking or touching stdout (the MCP
    channel): the opener's stdin/stdout/stderr go to /dev/null. ``reaper`` takes the wait for an
    opener that is still running after ``VIEWER_CHECK_S``, so it is not left a zombie.
    """
    if sys.platform == "win32":
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            return False, "os.startfile is unavailable"
        try:
            await asyncio.to_thread(startfile, str(path))
        except OSError as exc:
            return False, f"os.startfile failed: {exc}"
        return True, "opened with os.startfile"
    cmd = _viewer_command(path)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"could not run {cmd[0]}: {exc}"
    try:
        code = await asyncio.wait_for(asyncio.shield(proc.wait()), VIEWER_CHECK_S)
    except asyncio.TimeoutError:
        reaper(proc.wait())
        return True, f"opened with {cmd[0]}"
    if code != 0:
        return False, f"{cmd[0]} exited with status {code}"
    return True, f"opened with {cmd[0]}"


def _text_qr(uri: str) -> str:
    """The most compact text QR: half blocks (two module rows per line), error level L."""
    from walletconnect.qr import terminal_qr  # half blocks, level L, never Micro QR

    return terminal_qr(uri, compact=True, border=TEXT_QR_BORDER)


def _pair_text(result: dict[str, Any]) -> str:
    """The short text block of ``pair``: where the QR is, the topic and what to do next."""
    lines = []
    if "qr_svg" in result:
        lines.append(
            "Render this SVG QR code exactly as given (everything between the markers) with your"
            f" inline widget/artifact tool at a large size ({result['qr_svg_bytes']} bytes):"
        )
        lines.append(SVG_BEGIN)
        lines.append(result["qr_svg"])
        lines.append(SVG_END)
    if result.get("qr_opened"):
        lines.append(
            "The QR code was opened in an image viewer window on the user's screen; tell them to"
            f" scan it with their wallet app. File: {result['qr_path']}"
        )
        lines.append(f"file URI: {result['qr_file_uri']}")
    elif "qr_path" in result and "qr_svg" not in result:
        lines.append(f"Show this QR code to the user so they can scan it: {result['qr_path']}")
        lines.append(f"file URI: {result['qr_file_uri']}")
        lines.append(f"(not opened in a viewer: {result['qr_open_reason']})")
    if "qr" in result:
        lines.append(f"QR code:\n{result['qr']}")
    if "qr_error" in result:
        lines.append(result["qr_error"])
    lines.append(f"pairing_topic: {result['pairing_topic']}")
    lines.append(f"expires: {result['expires_at']} ({result['seconds_remaining']}s)")
    if "uri" in result:
        lines.append(f"uri (short-lived secret): {result['uri']}")
    lines.append(result["next"])
    return "\n".join(lines)


# ---------------------------------------------------------------- server


def create_server(client_factory: Callable[[], SignClient] | None = None) -> MCPServer:
    """Build the server. ``client_factory`` lets tests inject a client (e.g. a fake relay)."""
    holder: dict[str, SignClient] = {}
    pairings: dict[str, dict[str, Any]] = {}
    settlers: dict[str, asyncio.Task[Any]] = {}
    qr_files: dict[str, Path] = {}
    qr_pngs: dict[str, bytes] = {}
    tasks: set[asyncio.Task[Any]] = set()

    def track(awaitable: Awaitable[Any]) -> None:
        """Run ``awaitable`` in the background; cancelled at shutdown like the settlers."""

        async def run() -> None:
            with contextlib.suppress(Exception):
                await awaitable

        task = asyncio.ensure_future(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def forget_qr(pairing_topic: str) -> None:
        qr_pngs.pop(pairing_topic, None)
        path = qr_files.pop(pairing_topic, None)
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        client = (client_factory or default_client)()
        await client.open()  # resumes restored sessions
        holder["client"] = client
        logger.info("WalletConnect client %s ready", client.client_id)
        try:
            yield
        finally:
            for task in list(tasks):
                task.cancel()
            for pairing_topic in list(qr_files):
                forget_qr(pairing_topic)
            holder.pop("client", None)
            await client.close()

    server = MCPServer(name="walletconnect", instructions=INSTRUCTIONS, lifespan=lifespan)

    def current() -> SignClient:
        try:
            return holder["client"]
        except KeyError:
            raise ToolError("The WalletConnect client is not running") from None

    async def settle(pairing_topic: str, task: asyncio.Task[Any]) -> None:
        status = pairings[pairing_topic]
        try:
            session = await current().wait_for_session(task)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) or type(exc).__name__
            if isinstance(exc, WalletConnectTimeoutError):
                reason = f"timeout: {reason}. Call pair again for a new QR code."
            elif isinstance(exc, (UserRejectedError, UserRejectedRequestError)):
                reason = f"rejected: the user rejected the pairing in their wallet ({exc})"
            status.update(status="failed", error=reason)
        else:
            status.update(status="settled", session_topic=session.topic)
        finally:
            forget_qr(pairing_topic)

    def pairing_view(pairing_topic: str) -> dict[str, Any]:
        status = pairings.get(pairing_topic)
        if status is None:
            return {"pairing_topic": pairing_topic, "status": "unknown"}
        view = {k: v for k, v in status.items() if not k.startswith("_")}
        if view["status"] == "pending":
            view["seconds_remaining"] = max(0, int(status["_deadline"] - time.time()))
        return view

    def pick_session(client: SignClient, topic: str | None) -> Any:
        """The default-session rule: the given topic, else the only session, else an error."""
        if topic is None:
            sessions = list(client.sessions.values())
            if not sessions:
                raise ToolError("session_ended: no wallet is paired. Call `pair` first.")
            if len(sessions) > 1:
                listing = ", ".join(
                    f"{s.topic} ({s.peer_name or 'unnamed wallet'})" for s in sessions
                )
                raise ToolError(
                    f"ambiguous_session: {len(sessions)} sessions are open; pass `topic`, one of: "
                    f"{listing}"
                )
            return sessions[0]
        session = client.sessions.get(topic)
        if session is None:
            raise ToolError(
                f"session_ended: no open session with topic {topic} (it was disconnected, expired"
                " or never existed). See session_status, or call `pair` again."
            )
        return session

    def resolve(
        client: SignClient, topic: str | None, address: str | None, chain_id: str | int | None
    ) -> tuple[str, str, str]:
        """Pick the session, the signing address and the CAIP-2 chain (validated)."""
        session = pick_session(client, topic)
        accounts = session.eip155_accounts
        if not accounts:
            raise ToolError("invalid_session: the session has no eip155 accounts")
        if address is None:
            account = accounts[0]
            address = account.split(":")[-1]
        else:
            account = next((a for a in accounts if a.lower().endswith(address.lower())), None)
            if account is None:
                approved = sorted({a.split(":")[-1] for a in accounts})
                raise ToolError(
                    f"invalid_account: {address} is not in the session; approved: {approved}"
                )
        approved_chains = _approved_chains(session)
        if chain_id is None:
            chain = ":".join(account.split(":")[:2])
        else:
            chain = normalize_chain_id(chain_id)
            if chain not in approved_chains:
                raise ToolError(
                    f"unapproved_chain: {chain} is not approved in this session; approved chains:"
                    f" {sorted(approved_chains)}. Pair again to add a chain."
                )
        return session.topic, address, chain

    async def ask_wallet(client: SignClient, topic: str, request: Awaitable[Any]) -> str:
        """Wait for the wallet's answer, turning SDK errors into categorized messages."""
        try:
            result = await request
        except (NoActiveSessionError, SessionExpiredError, SessionDeletedError) as exc:
            raise ToolError(f"session_ended: {exc}. Pair again with `pair`.") from exc
        except WalletConnectTimeoutError as exc:
            hint = (
                " The wallet did not answer the last ping either, so the phone may be offline."
                if topic in client.unreachable
                else ""
            )
            raise ToolError(
                f"timeout: the user didn't respond in time ({exc}).{hint} The request may still be"
                " showing in the wallet; it cannot be withdrawn, so tell the user to reject it if"
                " it is no longer wanted. session_status(check_reachable=true) checks the wallet."
            ) from exc
        except (UserRejectedRequestError, UserRejectedError) as exc:
            raise ToolError(
                f"rejected: the user rejected the request in their wallet ({exc})"
            ) from exc
        except SdkError as exc:
            raise ToolError(f"wallet_error: the wallet returned error {exc.code}: {exc}") from exc
        except RelayConnectionError as exc:
            raise ToolError(f"relay_unavailable: lost the relay connection: {exc}") from exc
        except WalletConnectError as exc:
            raise ToolError(f"error: {exc}") from exc
        return str(result)

    def _timeout(timeout_s: float | None) -> float | None:
        if timeout_s is None:
            return None
        if timeout_s <= 0:
            raise ToolError("invalid_argument: timeout_s must be positive")
        return float(timeout_s)

    @server.tool()
    async def pair(
        qr: Literal["svg_inline", "png", "svg", "text", "none"] = "svg_inline",
        qr_path: str | None = None,
        include_uri: bool = False,
        include_image: bool = True,
        qr_scale: int | None = None,
        open_qr: bool | None = None,
    ) -> CallToolResult:
        """
        Start pairing a mobile wallet (asks for eip155:1). By default (``qr="svg_inline"``) the
        result text holds the QR code as SVG markup (one path, white background, square
        viewBox, a few KB) between -----BEGIN QR SVG----- and -----END QR SVG-----, and the wc:
        URI never appears (``include_uri`` is refused with it). ``open_qr=true`` (or
        ``WALLETCONNECT_OPEN_QR=1``) also writes and opens a PNG in the image viewer.

        ``qr="png"`` or ``"svg"`` writes the QR code to a file and returns its path as
        ``qr_path``. The file goes to
        ``qr_path`` if given, else ``$WALLETCONNECT_QR_DIR``, else the system temp dir, and is
        deleted once the pairing settles or fails. With ``include_image`` (PNG only) the QR is
        also attached as an image, about 600px wide; ``qr_scale`` (pixels per module, 1-20)
        overrides the size. ``open_qr`` opens the file in the system image viewer (default:
        ``$WALLETCONNECT_OPEN_QR``, else yes on macOS/Windows or when DISPLAY/WAYLAND_DISPLAY is
        set); the result's ``qr_opened`` and ``qr_open_reason`` say what happened. When it was not
        opened, give the user the qr_path: ``xdg-open <path>`` (Linux) or ``open <path>`` (macOS)
        shows it full size.
        ``qr="text"`` returns a compact Unicode QR inline instead; ``"none"`` makes no QR.
        The PNG is also readable as the resource ``walletconnect://pairing/<topic>/qr.png``
        while the pairing is pending. ``include_uri=true`` adds the ``wc:`` URI to the result:
        it contains a short-lived secret key, so only request it when the user asks to paste it.

        IMPORTANT: After pair, render the returned SVG exactly as given with your inline
        widget/artifact tool at a large size, tell the user to scan it with their wallet, then
        call wait_for_pairing. If you cannot render widgets, call pair again with qr="text" or
        qr="png". With qr="png": if qr_opened is true, tell the user the QR code opened in a
        window on their screen; otherwise show the QR code prominently in your reply (render the
        image, or give them the PNG path or file:// link). Never paste the wc: URI unless the
        user asks.
        """
        if qr_scale is not None and not QR_MIN_SCALE <= qr_scale <= QR_MAX_SCALE:
            raise ToolError(
                f"invalid_argument: qr_scale must be {QR_MIN_SCALE}-{QR_MAX_SCALE}"
                " pixels per module"
            )
        if include_uri and qr == "svg_inline":
            raise ToolError(
                "invalid_argument: qr='svg_inline' never puts the wc: URI in the transcript; drop"
                " include_uri, or use another qr mode if the user wants to paste the URI"
            )
        if not include_uri and qr == "none":
            raise ToolError("invalid_argument: qr='none' with include_uri=false returns nothing")
        client = current()
        uri, task = await client.connect(chains=(1,))
        pairing_topic = PairingURI.parse(uri).topic
        timeout = min(client.connect_timeout, float(PAIRING_PROPOSAL_EXPIRY))
        deadline = time.time() + timeout
        pairings[pairing_topic] = {
            "pairing_topic": pairing_topic,
            "status": "pending",
            "expires_at": _iso(deadline),
            "_deadline": deadline,
        }
        result: dict[str, Any] = {"pairing_topic": pairing_topic, "qr_format": qr}
        content: list[Any] = []
        try:
            if qr in ("png", "svg"):
                data, width = _render_qr_bytes(uri, qr, qr_scale)
                result["qr_width_px"] = width
                path = _qr_target(qr_path, pairing_topic, f".{qr}")
                _write_private(path, data)
                qr_files[pairing_topic] = path
                result["qr_path"] = str(path)
                result["qr_file_uri"] = path.as_uri()
                if qr == "png":
                    qr_pngs[pairing_topic] = data
                    resource_uri = QR_RESOURCE_TEMPLATE.format(pairing_topic=pairing_topic)
                    result["qr_resource"] = resource_uri
                    content.append(
                        ResourceLink(
                            type="resource_link",
                            name="walletconnect-pairing-qr",
                            title="WalletConnect pairing QR code",
                            uri=resource_uri,
                            mime_type="image/png",
                            size=len(data),
                        )
                    )
                if qr == "png" and include_image:
                    content.insert(
                        0,
                        ImageContent(
                            type="image",
                            data=base64.b64encode(data).decode(),
                            mime_type="image/png",
                        ),
                    )
            elif qr == "text":
                result["qr"] = _text_qr(uri)
            elif qr == "svg_inline":
                svg, modules = svg_inline_qr(uri)
                result["qr_svg"] = svg
                result["qr_svg_bytes"] = len(svg.encode())
                result["qr_modules"] = modules
                wanted, _ = _open_qr_decision(open_qr, inline=True)
                if wanted:  # explicitly asked for: also put a PNG on screen
                    data, width = _render_qr_bytes(uri, "png", qr_scale)
                    path = _qr_target(qr_path, pairing_topic, ".png")
                    _write_private(path, data)
                    qr_files[pairing_topic] = path
                    result["qr_path"] = str(path)
                    result["qr_file_uri"] = path.as_uri()
        except ImportError:
            if not include_uri:
                task.cancel()
                pairings.pop(pairing_topic, None)
                raise ToolError(
                    "qr_unavailable: QR rendering needs the 'qr' extra (segno); install it or"
                    " call pair with include_uri=true"
                ) from None
            result["qr_error"] = "QR rendering needs the 'qr' extra (segno); use the URI"
        except OSError as exc:
            task.cancel()
            pairings.pop(pairing_topic, None)
            raise ToolError(f"qr_write_failed: could not write the QR file: {exc}") from exc
        if "qr_path" in result:
            # The file stays until the pairing settles, fails or expires, so the viewer can load it.
            wanted, reason = _open_qr_decision(open_qr, inline=qr == "svg_inline")
            opened = False
            if wanted:
                opened, reason = await _open_in_viewer(Path(result["qr_path"]), track)
            result["qr_opened"] = opened
            result["qr_open_reason"] = reason
        else:
            result["qr_opened"] = False
            result["qr_open_reason"] = (
                _open_qr_decision(open_qr, inline=True)[1]
                if qr == "svg_inline"
                else f"no QR file (qr={qr!r})"
            )
        if include_uri:
            result["uri"] = uri
        result["expires_at"] = _iso(deadline)
        result["seconds_remaining"] = int(timeout)
        if qr == "svg_inline":
            result["next"] = SVG_INLINE_GUIDANCE + (
                " The QR code was also opened in an image viewer window."
                if result["qr_opened"]
                else ""
            )
        elif result["qr_opened"]:
            result["next"] = (
                "Tell the user: 'The QR code was opened in your image viewer; scan it with your"
                " wallet.' Then call wait_for_pairing with pairing_topic. Don't paste the wc: URI"
                " unless asked."
            )
        else:
            result["next"] = (
                "Show the QR code to the user prominently in your reply (render the image, or give"
                " them qr_path / qr_file_uri to open; `xdg-open` on Linux or `open` on macOS shows"
                " it full size) and tell them to scan it with their wallet app. Then call"
                " wait_for_pairing with pairing_topic. Don't paste the wc: URI unless asked."
            )
        bg = asyncio.create_task(settle(pairing_topic, task))
        settlers[pairing_topic] = bg
        tasks.add(bg)
        bg.add_done_callback(tasks.discard)
        content.insert(0, TextContent(type="text", text=_pair_text(result)))
        return CallToolResult(content=content, structured_content=result)

    @server.resource(
        QR_RESOURCE_TEMPLATE,
        name="walletconnect-pairing-qr",
        description="The PNG QR code of a pending pairing (gone once it settles or fails)",
        mime_type="image/png",
    )
    def pairing_qr(pairing_topic: str) -> bytes:
        try:
            return qr_pngs[pairing_topic]
        except KeyError:
            raise ValueError(
                f"No pending PNG QR code for pairing {pairing_topic}; call pair again"
            ) from None

    @server.tool()
    async def wait_for_pairing(
        pairing_topic: str, timeout_s: float = DEFAULT_WAIT_TIMEOUT
    ) -> dict[str, Any]:
        """
        Wait up to ``timeout_s`` seconds (max 300) for the wallet to approve a pairing started
        by ``pair``. Returns ``status``: ``settled`` (with ``session_topic`` and ``session``),
        ``failed`` (with ``error``), ``unknown``, or ``pending`` with ``seconds_remaining`` until
        the pairing expires; call it again while pending.
        """
        if timeout_s <= 0:
            raise ToolError("invalid_argument: timeout_s must be positive")
        bg = settlers.get(pairing_topic)
        if bg is not None and not bg.done():
            await asyncio.wait({bg}, timeout=min(float(timeout_s), MAX_WAIT_TIMEOUT))
        view = pairing_view(pairing_topic)
        if view["status"] == "settled":
            client = current()
            session = client.sessions.get(view["session_topic"])
            if session is not None:
                view["session"] = _session_info(client, session)
        return view

    @server.tool()
    async def session_status(
        pairing_topic: str | None = None, check_reachable: bool = False
    ) -> dict[str, Any]:
        """
        List the paired wallet sessions: topic, wallet name, accounts, approved chains, expiry
        (unix seconds, ``expiry_iso`` in UTC and ``expires_in_s``) and reachable. Pass the
        pairing_topic from ``pair`` to see whether that pairing is pending (with
        seconds_remaining), settled or failed. With check_reachable, ping each wallet first
        (takes up to 10s per offline wallet).
        """
        client = current()
        if check_reachable:
            await asyncio.gather(*(client.is_reachable(topic=t) for t in list(client.sessions)))
        result: dict[str, Any] = {
            "sessions": [_session_info(client, s) for s in client.sessions.values()]
        }
        if pairing_topic is not None:
            result["pairing"] = pairing_view(pairing_topic)
        return result

    @server.tool()
    async def disconnect(topic: str | None = None) -> dict[str, Any]:
        """
        End a session: tell the wallet and forget it. ``topic`` follows the same rule as the
        signing tools (optional when exactly one session is open).
        """
        client = current()
        session = pick_session(client, topic)
        try:
            await client.disconnect(topic=session.topic)
        except WalletConnectError as exc:
            raise ToolError(f"error: could not disconnect: {exc}") from exc
        return {
            "disconnected": session.topic,
            "peer_name": session.peer_name,
            "sessions_left": len(client.sessions),
        }

    @server.tool()
    async def sign_message(
        message: str,
        address: str | None = None,
        topic: str | None = None,
        chain_id: str | int | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """
        Ask the wallet to sign a text message (``personal_sign``). Blocks until the user approves
        or rejects on their phone, or ``timeout_s`` passes (default: the server's request
        timeout, 300s). ``address`` defaults to the session's first account; ``chain_id`` is
        CAIP-2 (``eip155:1``) or a bare number (``1``) and must be approved in the session.
        """
        client = current()
        topic, address, chain = resolve(client, topic, address, chain_id)
        msg_hex = "0x" + message.encode("utf-8").hex()
        signature = await ask_wallet(
            client,
            topic,
            client.request(
                "personal_sign",
                [msg_hex, address],
                chain_id=chain,
                topic=topic,
                timeout=_timeout(timeout_s),
            ),
        )
        return {"address": address, "chain_id": chain, "signature": signature}

    @server.tool()
    async def sign_typed_data(
        typed_data: dict[str, Any],
        address: str | None = None,
        topic: str | None = None,
        chain_id: str | int | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """
        Ask the wallet to sign EIP-712 typed data (``eth_signTypedData_v4``): an object with
        ``types``, ``primaryType``, ``domain`` and ``message``. ``types.EIP712Domain`` is added
        from the domain's fields when missing (and passed through when present). ``chain_id``
        (CAIP-2 or bare number, approved in the session) defaults to the account's chain; when
        ``domain.chainId`` differs from it the result has a ``warnings`` entry. Blocks like
        sign_message.
        """
        client = current()
        topic, address, chain = resolve(client, topic, address, chain_id)
        payload, added = with_eip712_domain(typed_data)
        warnings: list[str] = []
        domain_chain = payload["domain"].get("chainId")
        if domain_chain is not None:
            try:
                normalized = normalize_chain_id(
                    domain_chain if isinstance(domain_chain, (int, str)) else str(domain_chain)
                )
            except ToolError:
                warnings.append(f"domain.chainId {domain_chain!r} is not a valid chain id")
            else:
                if normalized != chain:
                    warnings.append(
                        f"domain.chainId is {normalized} but the request was sent on {chain}."
                        " The signature is only valid where the verifying contract's chain"
                        " matches domain.chainId."
                    )
        signature = await ask_wallet(
            client,
            topic,
            client.request(
                "eth_signTypedData_v4",
                [address, json.dumps(payload)],
                chain_id=chain,
                topic=topic,
                timeout=_timeout(timeout_s),
            ),
        )
        result: dict[str, Any] = {
            "address": address,
            "chain_id": chain,
            "signature": signature,
            "added_eip712_domain": added,
        }
        if warnings:
            result["warnings"] = warnings
        return result

    @server.tool()
    async def send_transaction(
        to: str,
        value_wei: str | int = "0",
        data: str | None = None,
        gas: str | int | None = None,
        from_address: str | None = None,
        topic: str | None = None,
        chain_id: str | int | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """
        Ask the wallet to send a transaction (``eth_sendTransaction``). The user reviews it and
        approves on their phone; the wallet signs and broadcasts it. Returns the tx hash only.
        ``value_wei`` and ``gas`` take an integer, a decimal string (``"1000000000000000000"``,
        safest for big values) or a ``0x`` hex string. ``data`` is 0x-prefixed calldata.
        ``chain_id`` is CAIP-2 or a bare number, approved in the session. Blocks like
        sign_message.
        """
        client = current()
        topic, sender, chain = resolve(client, topic, from_address, chain_id)
        tx: dict[str, str] = {
            "from": sender,
            "to": to,
            "value": hex(parse_quantity(value_wei, "value_wei")),
        }
        if data:
            tx["data"] = data
        if gas is not None:
            tx["gas"] = hex(parse_quantity(gas, "gas"))
        tx_hash = await ask_wallet(
            client,
            topic,
            client.request(
                "eth_sendTransaction",
                [tx],
                chain_id=chain,
                topic=topic,
                timeout=_timeout(timeout_s),
            ),
        )
        return {"from": sender, "chain_id": chain, "tx_hash": tx_hash}

    return server


def main() -> None:
    # stdout carries the MCP protocol; logs must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    create_server().run()  # stdio


if __name__ == "__main__":
    main()
