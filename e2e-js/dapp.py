#!/usr/bin/env python3
"""Python dApp side of the JS wallet E2E.

Writes the pairing URI to --uri-file, waits for the JS wallet to settle,
then issues personal_sign (and optionally eth_signTypedData_v4).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

# Prefer the local checkout (parent of e2e-js/).
REPO = Path(__file__).resolve().parents[1]
if (REPO / "walletconnect").is_dir():
    sys.path.insert(0, str(REPO))

from walletconnect.sign import SyncSignClient
from walletconnect.types import Metadata


TYPED_DATA = {
    "types": {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
        ],
        "Mail": [
            {"name": "from", "type": "string"},
            {"name": "to", "type": "string"},
            {"name": "contents", "type": "string"},
        ],
    },
    "primaryType": "Mail",
    "domain": {"name": "walletconnect-py", "version": "1", "chainId": 1},
    "message": {
        "from": "Python dApp",
        "to": "JS wallet",
        "contents": "E2E typed data",
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default=os.environ.get("WALLETCONNECT_PROJECT_ID"))
    parser.add_argument("--uri-file", required=True)
    parser.add_argument("--typed-data", action="store_true")
    parser.add_argument("--message", default="Hello from walletconnect-py E2E")
    parser.add_argument("--storage", default="/tmp/wc-e2e-dapp-storage")
    args = parser.parse_args()
    if not args.project_id:
        print("WALLETCONNECT_PROJECT_ID required", file=sys.stderr)
        return 1

    uri_path = Path(args.uri_file)
    if uri_path.exists():
        uri_path.unlink()

    from walletconnect.storage import KeyValueStorage

    client = SyncSignClient(
        project_id=args.project_id,
        metadata=Metadata(
            name="walletconnect-py e2e dApp",
            description="E2E against @walletconnect/sign-client wallet",
            url="https://github.com/ApeWorX/walletconnect-py",
            icons=[],
        ),
        storage=KeyValueStorage(args.storage),
        connect_timeout=90,
        request_timeout=60,
    )

    def on_uri(uri: str) -> None:
        uri_path.write_text(uri + "\n", encoding="utf-8")
        print(f"[dapp] wrote URI to {uri_path}", flush=True)
        print(f"[dapp] URI={uri}", flush=True)

    try:
        print("[dapp] connecting…", flush=True)
        session = client.connect(chains=(1,), on_uri=on_uri)
        accounts = session.eip155_accounts or session.accounts
        print(f"[dapp] session settled accounts={accounts}", flush=True)
        if not accounts:
            print("[dapp] no accounts in session", file=sys.stderr)
            return 1
        address = accounts[0].split(":")[-1]

        print(f"[dapp] personal_sign → {address}", flush=True)
        sig = client.personal_sign(args.message, address, chain_id="eip155:1")
        print(f"[dapp] personal_sign result={sig}", flush=True)
        if not (isinstance(sig, str) and sig.startswith("0x") and len(sig) >= 130):
            print("[dapp] unexpected personal_sign result", file=sys.stderr)
            return 1

        if args.typed_data:
            print("[dapp] eth_signTypedData_v4…", flush=True)
            typed_sig = client.request(
                "eth_signTypedData_v4",
                [address, json.dumps(TYPED_DATA)],
                chain_id="eip155:1",
            )
            print(f"[dapp] eth_signTypedData_v4 result={typed_sig}", flush=True)
            if not (
                isinstance(typed_sig, str) and typed_sig.startswith("0x") and len(typed_sig) >= 130
            ):
                print("[dapp] unexpected typed data result", file=sys.stderr)
                return 1

        print("[dapp] SUCCESS", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[dapp] FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        with contextlib.suppress(Exception):
            client.disconnect()
        client.close()
        # Keep URI file for debugging; orchestrator cleans up.


if __name__ == "__main__":
    raise SystemExit(main())
