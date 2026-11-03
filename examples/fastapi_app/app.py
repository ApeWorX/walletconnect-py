"""
FastAPI example: a small HTTP service that pairs mobile wallets and requests signatures.

Run it::

    export WALLETCONNECT_PROJECT_ID=your_32_char_id    # https://dashboard.walletconnect.com
    export WALLETCONNECT_STATE_FILE=./wc-state.json    # optional: survive restarts
    uv run --group examples uvicorn examples.fastapi_app.app:app --reload

Then::

    curl -X POST localhost:8000/pair                   # → {"uri": "wc:…", "pairing_topic": "…"}
    curl -X POST 'localhost:8000/pair?qr=svg'          # … plus an inline SVG QR code
    curl localhost:8000/pairings/<pairing_topic>       # pending → settled (with session topic)
    curl localhost:8000/sessions
    curl -X POST localhost:8000/sessions/<topic>/sign \\
         -H 'content-type: application/json' -d '{"message": "hello"}'
    curl -X DELETE localhost:8000/sessions/<topic>

This is an example, not a production service: it has no authentication, so anyone who can
reach it can ask the paired wallets to sign. Put it behind your own auth.

.. warning:: The state file holds secret keys (relay identity + session keys). It is written
   with ``0600`` permissions; keep it out of version control and backups you don't trust.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from walletconnect import DEFAULT_RELAY_URL, ClientState, Metadata, PairingURI
from walletconnect.exceptions import (
    NoActiveSessionError,
    SdkError,
    SessionExpiredError,
    WalletConnectError,
    WalletConnectTimeoutError,
)
from walletconnect.sign import SignClient

logger = logging.getLogger("walletconnect.examples.fastapi")

STATE_FILE_ENV = "WALLETCONNECT_STATE_FILE"
RELAY_URL_ENV = "WALLETCONNECT_RELAY_URL"

METADATA = Metadata(
    name="WalletConnect FastAPI example",
    description="Pair a wallet over HTTP and request signatures",
    url="https://github.com/ApeWorX/walletconnect-py",
)


# ----------------------------------------------------------------- schemas


class PairResponse(BaseModel):
    uri: str
    pairing_topic: str
    qr_svg: str | None = None


class PairingStatus(BaseModel):
    pairing_topic: str
    status: Literal["pending", "settled", "failed"]
    session_topic: str | None = None
    error: str | None = None


class SessionInfo(BaseModel):
    topic: str
    peer_name: str | None
    accounts: list[str]
    expiry: int


class SignRequest(BaseModel):
    method: Literal["personal_sign", "eth_signTypedData_v4"] = "personal_sign"
    message: str | None = Field(None, description="Text to sign (personal_sign)")
    typed_data: dict[str, Any] | None = Field(None, description="EIP-712 payload")
    address: str | None = Field(None, description="Defaults to the session's first account")
    chain_id: str | None = Field(None, description="CAIP-2, defaults to the account's chain")


class SignResponse(BaseModel):
    signature: str


# --------------------------------------------------------------------- app


def _session_info(session: Any) -> SessionInfo:
    return SessionInfo(
        topic=session.topic,
        peer_name=session.peer_name,
        accounts=session.accounts,
        expiry=session.expiry,
    )


def _state_path() -> Path | None:
    raw = os.environ.get(STATE_FILE_ENV)
    return Path(raw).expanduser() if raw else None


def create_app(client_factory: Callable[[], SignClient] | None = None) -> FastAPI:
    """Build the app. ``client_factory`` lets tests inject a client (e.g. a fake relay)."""

    def default_factory() -> SignClient:
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
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = (client_factory or default_factory)()
        app.state.client = client
        app.state.pairings = {}
        app.state.tasks = set()
        await client.open()  # resumes restored sessions
        logger.info("WalletConnect client %s ready", client.client_id)
        try:
            yield
        finally:
            for task in list(app.state.tasks):
                task.cancel()
            await client.close()

    app = FastAPI(title="WalletConnect FastAPI example", lifespan=lifespan)

    def client_of(request: Request) -> SignClient:
        return request.app.state.client  # type: ignore[no-any-return]

    async def settle(app: FastAPI, pairing_topic: str, task: asyncio.Task[Any]) -> None:
        status: PairingStatus = app.state.pairings[pairing_topic]
        try:
            session = await app.state.client.wait_for_session(task)
        except Exception as exc:  # noqa: BLE001
            status.status, status.error = "failed", str(exc) or type(exc).__name__
            logger.info("Pairing %s… failed: %s", pairing_topic[:8], status.error)
        else:
            status.status, status.session_topic = "settled", session.topic
            logger.info("Pairing %s… settled with %s", pairing_topic[:8], session.peer_name)

    @app.post("/pair")
    async def pair(request: Request, qr: Literal["svg"] | None = None) -> PairResponse:
        """Start a pairing. Returns immediately; the session settles in the background."""
        uri, task = await client_of(request).connect(chains=(1,))
        pairing_topic = PairingURI.parse(uri).topic
        request.app.state.pairings[pairing_topic] = PairingStatus(
            pairing_topic=pairing_topic, status="pending"
        )
        bg = asyncio.create_task(settle(request.app, pairing_topic, task))
        request.app.state.tasks.add(bg)
        bg.add_done_callback(request.app.state.tasks.discard)
        svg = None
        if qr == "svg":
            try:
                import segno
            except ImportError as exc:
                raise HTTPException(501, 'QR rendering needs "walletconnect[qr]"') from exc
            svg = segno.make(uri, error="m").svg_inline(scale=4)
        return PairResponse(uri=uri, pairing_topic=pairing_topic, qr_svg=svg)

    @app.get("/pairings/{pairing_topic}")
    async def pairing_status(request: Request, pairing_topic: str) -> PairingStatus:
        status = request.app.state.pairings.get(pairing_topic)
        if status is None:
            raise HTTPException(404, "Unknown pairing topic")
        return status  # type: ignore[no-any-return]

    @app.get("/sessions")
    async def list_sessions(request: Request) -> list[SessionInfo]:
        return [_session_info(s) for s in client_of(request).sessions.values()]

    @app.post("/sessions/{topic}/sign")
    async def sign(request: Request, topic: str, body: SignRequest) -> SignResponse:
        """Ask the wallet to sign; blocks until the user approves on their phone."""
        client = client_of(request)
        session = client.sessions.get(topic)
        if session is None:
            raise HTTPException(404, "Unknown or expired session")
        accounts = session.eip155_accounts
        if body.address is None and not accounts:
            raise HTTPException(400, "Session has no eip155 accounts")
        address = body.address or accounts[0].split(":")[-1]
        account = next((a for a in accounts if a.lower().endswith(address.lower())), None)
        chain_id = body.chain_id or (":".join(account.split(":")[:2]) if account else "eip155:1")
        try:
            if body.method == "personal_sign":
                if body.message is None:
                    raise HTTPException(422, "personal_sign needs 'message'")
                signature = await client.personal_sign(
                    body.message, address, chain_id=chain_id, topic=topic
                )
            else:
                if body.typed_data is None:
                    raise HTTPException(422, "eth_signTypedData_v4 needs 'typed_data'")
                signature = await client.request(
                    "eth_signTypedData_v4",
                    [address, json.dumps(body.typed_data)],
                    chain_id=chain_id,
                    topic=topic,
                )
        except NoActiveSessionError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SessionExpiredError as exc:
            raise HTTPException(410, str(exc)) from exc
        except WalletConnectTimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except SdkError as exc:  # e.g. the user rejected the request
            raise HTTPException(409, f"Wallet error {exc.code}: {exc}") from exc
        except WalletConnectError as exc:
            raise HTTPException(502, str(exc)) from exc
        return SignResponse(signature=str(signature))

    @app.delete("/sessions/{topic}", status_code=204)
    async def delete_session(request: Request, topic: str) -> None:
        client = client_of(request)
        if topic not in client.sessions:
            raise HTTPException(404, "Unknown or expired session")
        await client.disconnect("Disconnected via API", topic=topic)

    return app


app = create_app()
