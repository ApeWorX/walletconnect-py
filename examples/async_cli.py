#!/usr/bin/env python3
"""
Async Cyclopts CLI: pair a mobile wallet with SignClient, then personal_sign.

Usage:
  export WALLETCONNECT_PROJECT_ID=your_32_char_id   # from https://cloud.reown.com
  # or: set -a && source .env.local && set +a
  uv run --extra cli python examples/async_cli.py
  # or without installing the extra:
  uv run --with 'cyclopts>=3,<6' --extra qr python examples/async_cli.py

Scan the QR with MetaMask / Rainbow / etc., approve the session, then approve
personal_sign on your phone.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

app = App(
    name="walletconnect-async",
    help="Pair a mobile wallet via WalletConnect v2 using the async SignClient.",
    result_action="return_int_as_exit_code_else_zero",
)


def _load_dotenv_local() -> None:
    """Best-effort load of a gitignored ``.env.local`` (no python-dotenv dep)."""
    if os.environ.get("WALLETCONNECT_PROJECT_ID"):
        return
    here = Path.cwd().resolve()
    candidates = [here, *list(here.parents)[:4]]
    for base in candidates:
        path = base / ".env.local"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
        break


@app.default
async def connect(
    *,
    project_id: Annotated[
        str | None,
        Parameter(
            name=["--project-id", "-p"],
            help="Reown Cloud project ID (default: WALLETCONNECT_PROJECT_ID)",
        ),
    ] = None,
    chain: Annotated[int, Parameter(help="EVM chain id (default: 1)")] = 1,
    message: Annotated[
        str,
        Parameter(help="Message for personal_sign"),
    ] = "Hello from walletconnect-py",
    ascii_qr: Annotated[
        bool,
        Parameter(name="--ascii", help="Pure ASCII QR (##) instead of Unicode"),
    ] = False,
    origin: Annotated[
        str | None,
        Parameter(help="Origin header if the Reown project allowlists one"),
    ] = None,
) -> int:
    """Connect, show a terminal QR, wait for session, personal_sign, disconnect."""
    _load_dotenv_local()
    project_id = project_id or os.environ.get("WALLETCONNECT_PROJECT_ID")
    if not project_id:
        print(
            "Set WALLETCONNECT_PROJECT_ID or pass --project-id "
            "(create a free project at https://cloud.reown.com).",
            file=sys.stderr,
        )
        return 1

    from walletconnect.qr import print_qr
    from walletconnect.sign import SignClient
    from walletconnect.types import Metadata

    client = SignClient(
        project_id=project_id,
        metadata=Metadata(
            name="walletconnect-py async-cli",
            description="Cyclopts async SignClient demo",
            url="https://github.com/ApeWorX/walletconnect-py",
            icons=[],
        ),
        origin=origin,
    )
    try:
        uri, session_task = await client.connect(chains=(chain,))
        print("\nScan this WalletConnect URI with your mobile wallet:\n")
        print(uri)
        print()
        try:
            print_qr(uri, compact=not ascii_qr)
        except ImportError as exc:
            print(f"(QR skipped: {exc})", file=sys.stderr)
        print("\nWaiting for wallet approval…\n")

        session = await client.wait_for_session(session_task)
        accounts = session.eip155_accounts or session.accounts
        print(f"Connected. Accounts: {accounts}")
        if not accounts:
            print("No accounts in session namespaces; skipping personal_sign.")
            return 0

        address = accounts[0].split(":")[-1]
        print(f"Requesting personal_sign from {address} … approve on your phone.")
        signature = await client.personal_sign(message, address, chain_id=f"eip155:{chain}")
        print(f"Signature: {signature}")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
        with contextlib.suppress(Exception):
            await client.close()


if __name__ == "__main__":
    app()
