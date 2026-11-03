"""
MCP server example: let an AI assistant pair a mobile wallet and ask it to sign.

A small, readable demo of the SDK over stdio, using the official ``mcp`` Python SDK. Tools:
``pair``, ``wait_for_pairing``, ``session_status``, ``disconnect``, ``sign_message``,
``sign_typed_data`` and ``send_transaction``. Nothing is signed here: every request is approved
(or rejected) by the user in their wallet app.

Run it::

    export WALLETCONNECT_PROJECT_ID=your_32_char_id    # https://dashboard.walletconnect.com
    export WALLETCONNECT_STATE_FILE=./wc-mcp.json      # optional: survive restarts
    uv run --group examples --extra qr python -m examples.mcp_server

The production MCP integration is planned for the ``ape-walletconnect`` plugin.

.. warning:: The state file holds secret keys (relay identity + session keys). It is written
   with ``0600`` permissions; keep it out of version control.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from walletconnect import ClientState, Metadata, PairingURI
from walletconnect.constants import PAIRING_PROPOSAL_EXPIRY
from walletconnect.exceptions import (
    NoActiveSessionError,
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
QR_BORDER = 4  # quiet zone in modules, as the QR spec asks for

METADATA = Metadata(
    name="WalletConnect Python MCP Demo",
    description="An AI assistant asks your wallet to sign; you approve on your phone",
    url="https://github.com/ApeWorX/walletconnect-py",
)

SVG_BEGIN = "-----BEGIN QR SVG-----"
SVG_END = "-----END QR SVG-----"
SHOW_QR = (
    "After pair, render the returned SVG exactly as given with your inline widget/artifact tool"
    " at a large size, tell the user to scan it with their wallet, then call wait_for_pairing."
    ' If you cannot render widgets, call pair again with qr="text".'
)
INSTRUCTIONS = f"""\
Tools for a WalletConnect mobile wallet. Nothing is signed by this server: each signing tool sends
a request to the user's wallet app and waits until they approve or reject it there.
1. If session_status shows no sessions, call pair. It returns the QR code as SVG markup between
   {SVG_BEGIN} and {SVG_END}.
2. {SHOW_QR}
3. wait_for_pairing returns as soon as the wallet approves, or "pending"; call it again while
   pending.
4. Use sign_message, sign_typed_data or send_transaction and tell the user to check their phone.
   Errors start with a category such as "rejected:", "timeout:" or "session_ended:".
"""

# ---------------------------------------------------------------- the pairing QR


def make_qr(uri: str) -> Any:
    """Error level L exactly (no boost) and never Micro QR: the fewest modules that scan."""
    import segno

    return segno.make(uri, error="l", boost_error=False, micro=False)


def svg_inline_qr(uri: str) -> str:
    """
    The QR as compact SVG markup for the assistant to render (see docs/userguides/mcp.md).

    Every dark module is in ONE ``<path>``: a 1-unit stroke along each run of dark modules in a
    row, with relative moves. A white ``<rect>`` and a 4-module quiet zone keep it scannable in
    dark mode; the square viewBox, ``width="100%"`` and ``crispEdges`` let it scale up sharply.
    """
    matrix = make_qr(uri).matrix
    size = len(matrix) + 2 * QR_BORDER
    parts: list[str] = []
    pen: tuple[int, int] | None = None  # where the last run ended
    for y, row in enumerate(matrix, start=QR_BORDER):
        x = 0
        while x < len(row):
            if not row[x]:
                x += 1
                continue
            start = x
            while x < len(row) and row[x]:
                x += 1
            sx = start + QR_BORDER
            move = f"M{sx} {y}.5" if pen is None else f"m{sx - pen[0]} {y - pen[1]}"
            parts.append(f"{move}h{x - start}")
            pen = (x + QR_BORDER, y)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="100%"'
        f' shape-rendering="crispEdges"><rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path stroke="#000" d="{"".join(parts)}"/></svg>'
    )


def text_qr(uri: str) -> str:
    """Fallback: Unicode half blocks (two rows per line), level L, a 2-module quiet zone."""
    from walletconnect.qr import terminal_qr

    return terminal_qr(uri, compact=True, border=2)


# ---------------------------------------------------------------- helpers


def default_client() -> SignClient:
    """A client that keeps its sessions in ``$WALLETCONNECT_STATE_FILE`` when set."""
    raw = os.environ.get(STATE_FILE_ENV)
    path = Path(raw).expanduser() if raw else None
    state = ClientState.load(path) if path is not None and path.exists() else None

    def save(snapshot: ClientState) -> None:
        if path is not None:
            snapshot.save(path)  # written with 0600 permissions

    return SignClient(metadata=METADATA, state=state, on_state_change=save)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _int(raw: str) -> int:
    raw = raw.strip()
    return int(raw, 16) if raw.lower().startswith("0x") else int(raw, 10)


def normalize_chain_id(chain_id: str | int) -> str:
    """``"eip155:1"``, ``"1"``, ``1`` or ``"0x1"`` -> ``"eip155:1"``."""
    try:
        if isinstance(chain_id, bool):
            raise ValueError
        number = chain_id if isinstance(chain_id, int) else _int(chain_id.removeprefix("eip155:"))
    except ValueError:
        number = 0
    if number <= 0:
        raise ToolError(f"invalid_chain: {chain_id!r} is not a chain id like 'eip155:1' or 1")
    return f"eip155:{number}"


def parse_quantity(value: str | int, name: str) -> int:
    """An integer, decimal string or ``0x`` hex string (strings keep big values exact)."""
    try:
        if isinstance(value, bool):
            raise ValueError
        number = value if isinstance(value, int) else _int(value)
    except ValueError:
        raise ToolError(
            f"invalid_argument: {name}={value!r} must be an integer, a decimal string or 0x hex"
        ) from None
    if not 0 <= number < 2**256:
        raise ToolError(f"invalid_argument: {name} must be a uint256")
    return number


EIP712_DOMAIN_FIELDS = (
    ("name", "string"),
    ("version", "string"),
    ("chainId", "uint256"),
    ("verifyingContract", "address"),
    ("salt", "bytes32"),
)


def with_eip712_domain(typed_data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Add ``types.EIP712Domain`` from ``domain`` when missing; returns ``(data, added)``."""
    missing = {"types", "primaryType", "domain", "message"} - set(typed_data)
    if missing:
        raise ToolError(f"invalid_typed_data: missing {sorted(missing)}")
    if "EIP712Domain" in typed_data["types"]:
        return typed_data, False
    domain = typed_data["domain"]
    unknown = set(domain) - {name for name, _ in EIP712_DOMAIN_FIELDS}
    if unknown:
        raise ToolError(
            f"invalid_typed_data: domain has non-standard fields {sorted(unknown)}; "
            "pass types.EIP712Domain yourself"
        )
    result = copy.deepcopy(typed_data)
    fields = [{"name": n, "type": t} for n, t in EIP712_DOMAIN_FIELDS if n in domain]
    result["types"] = {"EIP712Domain": fields, **result["types"]}
    return result, True


def _chains(session: Any) -> set[str]:
    """The CAIP-2 chains a session approved."""
    ns = session.namespaces.get("eip155") or {}
    chains = {str(c) for c in ns.get("chains") or []}
    return chains | {":".join(a.split(":")[:2]) for a in ns.get("accounts") or []}


def _session_info(session: Any) -> dict[str, Any]:
    return {
        "topic": session.topic,
        "peer_name": session.peer_name,
        "accounts": session.accounts,
        "chains": sorted(_chains(session)),
        "expiry_iso": _iso(session.expiry),
    }


# ---------------------------------------------------------------- server


def create_server(client_factory: Callable[[], SignClient] | None = None) -> MCPServer:
    """Build the server. ``client_factory`` lets tests inject a client (e.g. a fake relay)."""
    holder: dict[str, SignClient] = {}
    pairings: dict[str, dict[str, Any]] = {}  # pairing topic -> status
    settlers: dict[str, asyncio.Task[Any]] = {}

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        client = (client_factory or default_client)()
        await client.open()  # resumes restored sessions
        holder["client"] = client
        try:
            yield
        finally:
            for task in settlers.values():
                task.cancel()
            await client.close()

    server = MCPServer(name="walletconnect", instructions=INSTRUCTIONS, lifespan=lifespan)

    def current() -> SignClient:
        if "client" not in holder:
            raise ToolError("The WalletConnect client is not running")
        return holder["client"]

    async def settle(pairing_topic: str, task: asyncio.Task[Any]) -> None:
        try:
            session = await current().wait_for_session(task)
        except (UserRejectedError, UserRejectedRequestError) as exc:
            pairings[pairing_topic].update(status="failed", error=f"rejected: {exc}")
        except Exception as exc:  # noqa: BLE001 (timeout or relay error: call pair again)
            reason = f"{exc or type(exc).__name__}. Call pair again for a new QR code."
            pairings[pairing_topic].update(status="failed", error=reason)
        else:
            pairings[pairing_topic].update(status="settled", session_topic=session.topic)

    def resolve(topic: str | None, chain_id: str | int | None) -> tuple[Any, str, str]:
        """The session (``topic``, else the only one), its first account and the chain."""
        client = current()
        sessions = list(client.sessions.values())
        if topic is not None:
            sessions = [s for s in sessions if s.topic == topic]
        if not sessions:
            raise ToolError("session_ended: no matching wallet session. Call `pair` first.")
        if len(sessions) > 1:
            topics = ", ".join(s.topic for s in sessions)
            raise ToolError(f"ambiguous_session: pass `topic`, one of: {topics}")
        session = sessions[0]
        if not session.eip155_accounts:
            raise ToolError("invalid_session: the session has no eip155 accounts")
        account = session.eip155_accounts[0]
        chain = ":".join(account.split(":")[:2])
        if chain_id is not None:
            chain = normalize_chain_id(chain_id)
            if chain not in _chains(session):
                raise ToolError(
                    f"unapproved_chain: {chain} is not approved in this session; approved:"
                    f" {sorted(_chains(session))}. Pair again to add a chain."
                )
        return session, account.split(":")[-1], chain

    async def ask_wallet(request: Awaitable[Any]) -> str:
        """Wait for the wallet's answer, turning SDK errors into categorized messages."""
        try:
            return str(await request)
        except (NoActiveSessionError, SessionExpiredError, SessionDeletedError) as exc:
            raise ToolError(f"session_ended: {exc}. Pair again with `pair`.") from exc
        except WalletConnectTimeoutError as exc:
            raise ToolError(
                f"timeout: the user didn't respond in time ({exc}). The request may still show"
                " in the wallet; tell the user to reject it if it is no longer wanted."
            ) from exc
        except (UserRejectedRequestError, UserRejectedError) as exc:
            raise ToolError(f"rejected: the user rejected the request ({exc})") from exc
        except SdkError as exc:
            raise ToolError(f"wallet_error: the wallet returned error {exc.code}: {exc}") from exc
        except WalletConnectError as exc:
            raise ToolError(f"error: {exc}") from exc

    @server.tool()
    async def pair(qr: Literal["svg_inline", "text"] = "svg_inline") -> CallToolResult:
        """
        Start pairing a mobile wallet (asks for eip155:1). Returns the QR code as SVG markup
        between -----BEGIN QR SVG----- and -----END QR SVG----- (``qr_svg``), or with
        ``qr="text"`` as a Unicode QR (``qr_text``). The wc: URI itself is never returned: it
        holds a short-lived secret key.

        IMPORTANT: After pair, render the returned SVG exactly as given with your inline
        widget/artifact tool at a large size, tell the user to scan it with their wallet, then
        call wait_for_pairing. If you cannot render widgets, call pair again with qr="text".
        """
        try:
            import segno  # noqa: F401  (the 'qr' extra)
        except ImportError:
            raise ToolError("qr_unavailable: install the 'qr' extra (segno)") from None
        uri, task = await current().connect(chains=(1,))
        pairing_topic = PairingURI.parse(uri).topic
        deadline = time.time() + min(current().connect_timeout, PAIRING_PROPOSAL_EXPIRY)
        pairings[pairing_topic] = {"pairing_topic": pairing_topic, "status": "pending"}
        settlers[pairing_topic] = asyncio.create_task(settle(pairing_topic, task))
        result: dict[str, Any] = {"pairing_topic": pairing_topic, "expires_at": _iso(deadline)}
        if qr == "svg_inline":
            result["qr_svg"] = svg = svg_inline_qr(uri)
            result["next"] = SHOW_QR
            qr_block = f"Render this SVG QR code exactly as given:\n{SVG_BEGIN}\n{svg}\n{SVG_END}"
        else:
            result["qr_text"] = qr_block = text_qr(uri)
            result["next"] = "Show this QR code to the user, then call wait_for_pairing."
        # Plain text, not JSON, so the markup can be copied verbatim (no escaped quotes).
        text = f"{qr_block}\npairing_topic: {pairing_topic}\nexpires: {result['expires_at']}"
        text += f"\n{result['next']}"
        return CallToolResult(
            content=[TextContent(type="text", text=text)], structured_content=result
        )

    @server.tool()
    async def wait_for_pairing(pairing_topic: str, timeout_s: float = 60) -> dict[str, Any]:
        """
        Wait up to ``timeout_s`` seconds (max 300) for the wallet to approve a pairing started
        by ``pair``. Returns ``status``: ``settled`` (with the session), ``failed`` (with
        ``error``), ``unknown``, or ``pending``; call it again while pending.
        """
        task = settlers.get(pairing_topic)
        if task is None:
            return {"pairing_topic": pairing_topic, "status": "unknown"}
        await asyncio.wait({task}, timeout=max(0.0, min(timeout_s, PAIRING_PROPOSAL_EXPIRY)))
        status = dict(pairings[pairing_topic])
        session = current().sessions.get(status.get("session_topic", ""))
        if session is not None:
            status["session"] = _session_info(session)
        return status

    @server.tool()
    async def session_status() -> dict[str, Any]:
        """List the paired wallet sessions: topic, wallet name, accounts, chains and expiry."""
        return {"sessions": [_session_info(s) for s in current().sessions.values()]}

    @server.tool()
    async def disconnect(topic: str | None = None) -> dict[str, Any]:
        """End a session (``topic`` is optional when only one is open)."""
        session, _, _ = resolve(topic, None)
        await ask_wallet(current().disconnect(topic=session.topic))
        return {"disconnected": session.topic, "sessions_left": len(current().sessions)}

    @server.tool()
    async def sign_message(
        message: str, topic: str | None = None, chain_id: str | int | None = None
    ) -> dict[str, Any]:
        """
        Ask the wallet to sign a text message (``personal_sign``) with the session's first
        account. Blocks until the user approves or rejects on their phone. ``chain_id`` is
        CAIP-2 (``eip155:1``) or a number, and must be approved in the session.
        """
        session, address, chain = resolve(topic, chain_id)
        params = ["0x" + message.encode().hex(), address]
        signature = await ask_wallet(
            current().request("personal_sign", params, chain_id=chain, topic=session.topic)
        )
        return {"address": address, "chain_id": chain, "signature": signature}

    @server.tool()
    async def sign_typed_data(
        typed_data: dict[str, Any], topic: str | None = None, chain_id: str | int | None = None
    ) -> dict[str, Any]:
        """
        Ask the wallet to sign EIP-712 typed data (``eth_signTypedData_v4``): an object with
        ``types``, ``primaryType``, ``domain`` and ``message``. ``types.EIP712Domain`` is added
        from the domain when missing. Blocks like sign_message.
        """
        session, address, chain = resolve(topic, chain_id)
        payload, added = with_eip712_domain(typed_data)
        params = [address, json.dumps(payload)]
        signature = await ask_wallet(
            current().request("eth_signTypedData_v4", params, chain_id=chain, topic=session.topic)
        )
        return {
            "address": address,
            "chain_id": chain,
            "signature": signature,
            "added_eip712_domain": added,
        }

    @server.tool()
    async def send_transaction(
        to: str,
        value_wei: str | int = "0",
        data: str | None = None,
        topic: str | None = None,
        chain_id: str | int | None = None,
    ) -> dict[str, Any]:
        """
        Ask the wallet to send a transaction (``eth_sendTransaction``); the user reviews and
        approves it on their phone, and the wallet signs and broadcasts it. ``value_wei`` is an
        integer, a decimal string (safest for big values) or 0x hex. Returns the tx hash.
        """
        session, sender, chain = resolve(topic, chain_id)
        tx = {"from": sender, "to": to, "value": hex(parse_quantity(value_wei, "value_wei"))}
        if data:
            tx["data"] = data
        tx_hash = await ask_wallet(
            current().request("eth_sendTransaction", [tx], chain_id=chain, topic=session.topic)
        )
        return {"from": sender, "chain_id": chain, "tx_hash": tx_hash}

    return server


def main() -> None:
    # stdout carries the MCP protocol; logs must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    create_server().run()  # stdio


if __name__ == "__main__":
    main()
