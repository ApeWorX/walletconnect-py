"""
MCP server example: let an AI assistant pair a mobile wallet and ask it to sign.

Uses the official ``mcp`` Python SDK (``MCPServer``, formerly ``FastMCP``) over stdio. The
assistant gets five tools: ``pair``, ``session_status``, ``sign_message``, ``sign_typed_data``
and ``send_transaction``. Nothing is signed here: every request is approved (or rejected) by the
user in their wallet app.

Run it::

    export WALLETCONNECT_PROJECT_ID=your_32_char_id    # https://dashboard.walletconnect.com
    export WALLETCONNECT_STATE_FILE=./wc-mcp.json      # optional: survive restarts
    uv run --group examples python -m examples.mcp_server

This is an example. The production MCP integration is planned as a feature of the
``ape-walletconnect`` plugin, so any Ape-based MCP server can reuse the same tools.

.. warning:: The state file holds secret keys (relay identity + session keys). It is written
   with ``0600`` permissions; keep it out of version control and backups you don't trust.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from walletconnect import DEFAULT_RELAY_URL, ClientState, Metadata, PairingURI
from walletconnect.exceptions import (
    NoActiveSessionError,
    SdkError,
    SessionDeletedError,
    SessionExpiredError,
    WalletConnectError,
    WalletConnectTimeoutError,
)
from walletconnect.sign import SignClient

logger = logging.getLogger("walletconnect.examples.mcp")

STATE_FILE_ENV = "WALLETCONNECT_STATE_FILE"
RELAY_URL_ENV = "WALLETCONNECT_RELAY_URL"
REQUEST_TIMEOUT_ENV = "WALLETCONNECT_REQUEST_TIMEOUT"
DEFAULT_REQUEST_TIMEOUT = 300.0  # the user has to notice the request on their phone

METADATA = Metadata(
    name="WalletConnect Python MCP Demo",
    description="An AI assistant asks your wallet to sign; you approve on your phone",
    url="https://github.com/ApeWorX/walletconnect-py",
)

INSTRUCTIONS = """\
Tools for a WalletConnect mobile wallet. Nothing is signed by this server: each signing tool sends
a request to the user's wallet app and waits until they approve or reject it there.
1. If session_status shows no sessions, call pair and show the user the QR code or URI to scan.
2. Poll session_status (with the pairing_topic) until the pairing is settled.
3. Use sign_message, sign_typed_data or send_transaction. Tell the user to check their phone.
"""


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


def _session_info(client: SignClient, session: Any) -> dict[str, Any]:
    return {
        "topic": session.topic,
        "peer_name": session.peer_name,
        "accounts": session.accounts,
        "expiry": session.expiry,
        "reachable": session.topic not in client.unreachable,
    }


def _render_qr(uri: str, kind: str) -> str | None:
    if kind == "none":
        return None
    try:
        import segno
    except ImportError:
        return None  # the "qr" extra isn't installed; the URI alone still works
    if kind == "svg":
        import io

        buf = io.BytesIO()
        segno.make(uri, error="m").save(buf, kind="svg", scale=4, xmldecl=False, nl=False)
        return buf.getvalue().decode("utf-8")
    from walletconnect.qr import terminal_qr

    return terminal_qr(uri)


def create_server(client_factory: Callable[[], SignClient] | None = None) -> MCPServer:
    """Build the server. ``client_factory`` lets tests inject a client (e.g. a fake relay)."""
    holder: dict[str, SignClient] = {}
    pairings: dict[str, dict[str, Any]] = {}
    tasks: set[asyncio.Task[Any]] = set()

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
            status.update(status="failed", error=str(exc) or type(exc).__name__)
        else:
            status.update(status="settled", session_topic=session.topic)

    def resolve(
        client: SignClient, topic: str | None, address: str | None, chain_id: str | None
    ) -> tuple[str, str, str]:
        """Pick the session, the signing address and the CAIP-2 chain."""
        if topic is None:
            if not client.sessions:
                raise ToolError("No wallet is paired. Call `pair` first.")
            if len(client.sessions) > 1:
                raise ToolError("Several sessions are open; pass `topic` (see session_status).")
            topic = next(iter(client.sessions))
        session = client.sessions.get(topic)
        if session is None:
            raise ToolError(f"Unknown or expired session {topic}")
        accounts = session.eip155_accounts
        if address is None and not accounts:
            raise ToolError("The session has no eip155 accounts")
        address = address or accounts[0].split(":")[-1]
        account = next((a for a in accounts if a.lower().endswith(address.lower())), None)
        if chain_id is None:
            chain_id = ":".join(account.split(":")[:2]) if account else "eip155:1"
        return topic, address, chain_id

    async def ask_wallet(request: Awaitable[Any]) -> str:
        """Wait for the wallet's answer, turning SDK errors into messages for the model."""
        try:
            result = await request
        except (NoActiveSessionError, SessionExpiredError, SessionDeletedError) as exc:
            raise ToolError(f"{exc} Pair again with `pair`.") from exc
        except WalletConnectTimeoutError as exc:
            raise ToolError(f"The user didn't respond in time: {exc}") from exc
        except SdkError as exc:  # e.g. the user rejected the request
            raise ToolError(f"The wallet returned error {exc.code}: {exc}") from exc
        except WalletConnectError as exc:
            raise ToolError(str(exc)) from exc
        return str(result)

    @server.tool()
    async def pair(qr: Literal["text", "svg", "none"] = "text") -> dict[str, Any]:
        """
        Start pairing a mobile wallet. Returns a ``wc:`` URI and a QR code to show the user
        (``text``: Unicode blocks for a terminal, ``svg``: an SVG image, ``none``: URI only).
        Then poll session_status with the returned pairing_topic until it is ``settled``.
        """
        uri, task = await current().connect(chains=(1,))
        pairing_topic = PairingURI.parse(uri).topic
        pairings[pairing_topic] = {"pairing_topic": pairing_topic, "status": "pending"}
        bg = asyncio.create_task(settle(pairing_topic, task))
        tasks.add(bg)
        bg.add_done_callback(tasks.discard)
        return {
            "uri": uri,
            "pairing_topic": pairing_topic,
            "qr_format": qr,
            "qr": _render_qr(uri, qr),
            "next": "Ask the user to scan the QR code (or paste the URI) in their wallet app.",
        }

    @server.tool()
    async def session_status(
        pairing_topic: str | None = None, check_reachable: bool = False
    ) -> dict[str, Any]:
        """
        List the paired wallet sessions (topic, wallet name, accounts, expiry). Pass the
        pairing_topic from ``pair`` to see whether that pairing is pending, settled or failed.
        With check_reachable, ping each wallet first (takes up to 10s per offline wallet).
        """
        client = current()
        if check_reachable:
            await asyncio.gather(*(client.is_reachable(topic=t) for t in list(client.sessions)))
        result: dict[str, Any] = {
            "sessions": [_session_info(client, s) for s in client.sessions.values()]
        }
        if pairing_topic is not None:
            result["pairing"] = pairings.get(
                pairing_topic, {"pairing_topic": pairing_topic, "status": "unknown"}
            )
        return result

    @server.tool()
    async def sign_message(
        message: str,
        address: str | None = None,
        topic: str | None = None,
        chain_id: str | None = None,
    ) -> dict[str, str]:
        """
        Ask the wallet to sign a text message (``personal_sign``). Blocks until the user approves
        on their phone. ``address`` defaults to the session's first account.
        """
        client = current()
        topic, address, chain_id = resolve(client, topic, address, chain_id)
        signature = await ask_wallet(
            client.personal_sign(message, address, chain_id=chain_id, topic=topic)
        )
        return {"address": address, "signature": signature}

    @server.tool()
    async def sign_typed_data(
        typed_data: dict[str, Any],
        address: str | None = None,
        topic: str | None = None,
        chain_id: str | None = None,
    ) -> dict[str, str]:
        """
        Ask the wallet to sign EIP-712 typed data (``eth_signTypedData_v4``): an object with
        ``types``, ``primaryType``, ``domain`` and ``message``. Blocks until the user approves.
        """
        client = current()
        topic, address, chain_id = resolve(client, topic, address, chain_id)
        params = [address, json.dumps(typed_data)]
        signature = await ask_wallet(
            client.request("eth_signTypedData_v4", params, chain_id=chain_id, topic=topic)
        )
        return {"address": address, "signature": signature}

    @server.tool()
    async def send_transaction(
        to: str,
        value_wei: int = 0,
        data: str | None = None,
        gas: int | None = None,
        from_address: str | None = None,
        topic: str | None = None,
        chain_id: str | None = None,
    ) -> dict[str, str]:
        """
        Ask the wallet to send a transaction (``eth_sendTransaction``). The user reviews it and
        approves on their phone; the wallet signs and broadcasts it. Returns the tx hash.
        ``data`` is 0x-prefixed calldata; ``value_wei`` is in wei.
        """
        client = current()
        topic, sender, chain_id = resolve(client, topic, from_address, chain_id)
        tx: dict[str, str] = {"from": sender, "to": to, "value": hex(value_wei)}
        if data:
            tx["data"] = data
        if gas is not None:
            tx["gas"] = hex(gas)
        tx_hash = await ask_wallet(
            client.request("eth_sendTransaction", [tx], chain_id=chain_id, topic=topic)
        )
        return {"from": sender, "chain_id": chain_id, "tx_hash": tx_hash}

    return server


def main() -> None:
    # stdout carries the MCP protocol; logs must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    create_server().run()  # stdio


if __name__ == "__main__":
    main()
