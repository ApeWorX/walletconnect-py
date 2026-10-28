#!/usr/bin/env python3
"""
Connect a Python terminal to a mobile WalletConnect v2 wallet.

Usage:
  export WALLETCONNECT_PROJECT_ID=your_32_char_id   # from https://cloud.reown.com
  uv run --extra qr python examples/terminal_connect.py
  # or: pip install 'walletconnect[qr]' && python examples/terminal_connect.py

Scan the QR with MetaMask / Rainbow / etc., approve the session, then approve the
personal_sign prompt on your phone.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default=os.environ.get("WALLETCONNECT_PROJECT_ID"))
    parser.add_argument("--chain", type=int, default=1, help="EVM chain id (default: 1)")
    parser.add_argument(
        "--ascii", action="store_true", help="Pure ASCII QR (##) instead of Unicode"
    )
    parser.add_argument(
        "--message",
        default="Hello from walletconnect-py",
        help="Message for personal_sign",
    )
    parser.add_argument(
        "--origin", default=None, help="Origin header if the Reown project allowlists one"
    )
    args = parser.parse_args()

    if not args.project_id:
        print(
            "Set WALLETCONNECT_PROJECT_ID or pass --project-id "
            "(create a free project at https://cloud.reown.com).",
            file=sys.stderr,
        )
        return 1

    from walletconnect.qr import print_qr
    from walletconnect.sign import SyncSignClient
    from walletconnect.types import Metadata

    def show_uri(uri: str) -> None:
        print("\nScan this WalletConnect URI with your mobile wallet:\n")
        print(uri)
        print()
        try:
            print_qr(uri, compact=not args.ascii)
        except ImportError as exc:
            print(f"(QR skipped: {exc})", file=sys.stderr)
        print("\nWaiting for wallet approval…\n")

    client = SyncSignClient(
        project_id=args.project_id,
        metadata=Metadata(
            name="walletconnect-py terminal",
            description="Manual connect demo",
            url="https://github.com/ApeWorX/walletconnect-py",
            icons=[],
        ),
        origin=args.origin,
    )
    try:
        session = client.connect(chains=(args.chain,), on_uri=show_uri)
        accounts = session.eip155_accounts or session.accounts
        print(f"Connected. Accounts: {accounts}")
        if not accounts:
            print("No accounts in session namespaces; skipping personal_sign.")
            return 0
        address = accounts[0].split(":")[-1]
        print(f"Requesting personal_sign from {address} … approve on your phone.")
        signature = client.personal_sign(args.message, address, chain_id=f"eip155:{args.chain}")
        print(f"Signature: {signature}")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
