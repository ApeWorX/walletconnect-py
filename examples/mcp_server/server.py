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
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

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
DEFAULT_REQUEST_TIMEOUT = 300.0  # the user has to notice the request on their phone
DEFAULT_WAIT_TIMEOUT = 60.0
MAX_WAIT_TIMEOUT = float(PAIRING_PROPOSAL_EXPIRY)

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

INSTRUCTIONS = """\
Tools for a WalletConnect mobile wallet. Nothing is signed by this server: each signing tool sends
a request to the user's wallet app and waits until they approve or reject it there.
1. If session_status shows no sessions, call pair. It writes a QR image to a file and returns its
   path (qr_path) plus the image itself. Show or point the user to it.
2. Call wait_for_pairing with the pairing_topic. It returns as soon as the wallet approves, or
   "pending" with seconds_remaining; call it again while pending.
3. Use sign_message, sign_typed_data or send_transaction. Tell the user to check their phone.
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


def _render_qr_bytes(uri: str, kind: Literal["png", "svg"]) -> bytes:
    import segno  # PNG and SVG are written by segno itself: no Pillow needed

    buf = io.BytesIO()
    qr = segno.make(uri, error="m")
    if kind == "png":
        qr.save(buf, kind="png", scale=6, border=4)
    else:
        qr.save(buf, kind="svg", scale=6, border=4, xmldecl=False, nl=False)
    return buf.getvalue()


# ---------------------------------------------------------------- server


def create_server(client_factory: Callable[[], SignClient] | None = None) -> MCPServer:
    """Build the server. ``client_factory`` lets tests inject a client (e.g. a fake relay)."""
    holder: dict[str, SignClient] = {}
    pairings: dict[str, dict[str, Any]] = {}
    settlers: dict[str, asyncio.Task[Any]] = {}
    qr_files: dict[str, Path] = {}
    tasks: set[asyncio.Task[Any]] = set()

    def forget_qr(pairing_topic: str) -> None:
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
        qr: Literal["png", "svg", "text", "none"] = "png",
        qr_path: str | None = None,
        include_uri: bool = True,
        include_image: bool = True,
    ) -> CallToolResult:
        """
        Start pairing a mobile wallet (asks for eip155:1). Writes the QR code to a file and
        returns its path as ``qr_path`` (``png`` default, or ``svg``). The file goes to
        ``qr_path`` if given, else ``$WALLETCONNECT_QR_DIR``, else the system temp dir, and is
        deleted once the pairing settles or fails. ``qr="text"`` returns Unicode blocks inline
        instead; ``"none"`` makes no QR. With ``include_image`` (PNG only) the QR is also
        attached as an image. ``include_uri=false`` leaves the ``wc:`` URI out of the result:
        it contains a short-lived secret key, so only include it if the user needs to paste it.
        Then call wait_for_pairing with the returned pairing_topic.
        """
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
                data = _render_qr_bytes(uri, qr)
                path = _qr_target(qr_path, pairing_topic, f".{qr}")
                _write_private(path, data)
                qr_files[pairing_topic] = path
                result["qr_path"] = str(path)
                if qr == "png" and include_image:
                    content.append(
                        ImageContent(
                            type="image",
                            data=base64.b64encode(data).decode(),
                            mime_type="image/png",
                        )
                    )
            elif qr == "text":
                from walletconnect.qr import terminal_qr

                result["qr"] = terminal_qr(uri)
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
        if include_uri:
            result["uri"] = uri
        result["expires_at"] = _iso(deadline)
        result["seconds_remaining"] = int(timeout)
        result["next"] = (
            "Show the user the QR code (or the file at qr_path) to scan in their wallet app"
            + (", or let them paste the URI" if include_uri else "")
            + ". Then call wait_for_pairing with pairing_topic."
        )
        bg = asyncio.create_task(settle(pairing_topic, task))
        settlers[pairing_topic] = bg
        tasks.add(bg)
        bg.add_done_callback(tasks.discard)
        content.insert(0, TextContent(type="text", text=json.dumps(result, indent=2)))
        return CallToolResult(content=content, structured_content=result)

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
