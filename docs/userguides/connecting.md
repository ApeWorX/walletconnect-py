# Connecting to a Mobile Wallet

`walletconnect` implements the dApp (proposer) side of the WalletConnect v2 / Reown Sign API.
Your Python script proposes a session, the user scans a QR code with their mobile wallet,
and once the wallet approves you can send JSON-RPC requests (like `personal_sign`) for the
wallet to approve on the phone.

## Requirements

- A project ID from [Reown Cloud](https://cloud.reown.com). Pass it as `project_id=` or set the
  `WALLETCONNECT_PROJECT_ID` environment variable.
- The optional `qr` extra if you want terminal QR codes: `pip install "walletconnect[qr]"`.

## Synchronous client

`SyncSignClient` runs the relay connection on a background event loop, so it works from plain
scripts, the REPL, and CLIs.

```python
from walletconnect.qr import print_qr
from walletconnect.sign import SyncSignClient
from walletconnect.types import Metadata

client = SyncSignClient(
    metadata=Metadata(
        name="my-script",
        description="Signs things from Python",
        url="https://example.com",
        icons=[],
    ),
)

# Prints a QR code to the terminal, then blocks until the wallet approves.
session = client.connect(chains=(1,), on_uri=print_qr)

address = session.eip155_accounts[0].split(":")[-1]
signature = client.personal_sign("hello from Python", address)

client.disconnect()
client.close()
```

`connect()` accepts the chain IDs to request (EIP-155 integers), plus optional `methods` and
`events`. All namespaces are sent as optional namespaces, as Reown recommends.

## Async client

`SignClient` exposes the same flow for asyncio applications. `connect()` returns the pairing URI
and a task that resolves once the wallet settles the session.

```python
import asyncio

from walletconnect.qr import print_qr
from walletconnect.sign import SignClient


async def main() -> None:
    client = SignClient()
    uri, session_task = await client.connect(chains=(1, 10))
    print_qr(uri)
    session = await client.wait_for_session(session_task)
    print(session.accounts)
    await client.close()


asyncio.run(main())
```

## Sending requests

Use `request(method, params, chain_id=...)` for any JSON-RPC method the wallet approved in the
session, for example `eth_sendTransaction` or `eth_signTypedData_v4`.
`personal_sign` is a convenience wrapper that hex-encodes the message for you.

## Terminal QR codes

`walletconnect.qr.print_qr` draws a QR code with Unicode half blocks. It is meant for testing
and for headless bots and scripts.

- Pass `invert=False` on light terminal backgrounds.
- Pass `compact=False` for pure ASCII output.
- Use `terminal_qr()` to get the string instead of printing it.

You can always print the raw `wc:` URI too and paste it into a wallet that supports it.

## Storage

The client keeps its relay identity key and session data in a small JSON key-value store under the
user data directory (see `walletconnect.storage.default_storage_path`). Pass your own
`KeyValueStorage(path)` to isolate scripts or tests.

## Relay

The default relay is `wss://relay.walletconnect.org`. Pass `relay_url=` to use another one.

## Examples

The repository ships two runnable demos:

- `examples/terminal_connect.py` uses `SyncSignClient`.
- `examples/async_cli.py` is an async [Cyclopts](https://cyclopts.readthedocs.io) CLI (install the
  `cli` extra).
