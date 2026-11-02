# Connecting to a Mobile Wallet

`walletconnect` implements the dApp (proposer) side of the WalletConnect v2 / Reown Sign API.
Your Python script proposes a session, the user scans a QR code with their mobile wallet,
and once the wallet approves you can send JSON-RPC requests (like `personal_sign`) for the
wallet to approve on the phone.

## Requirements

- A project ID. For connection-only use (like this library) create it on the
  [WalletConnect Dashboard](https://dashboard.walletconnect.com); IDs created on the older
  Reown Cloud / [Reown Dashboard](https://dashboard.reown.com) work too. Pass it as `project_id=`
  or set the `WALLETCONNECT_PROJECT_ID` environment variable.
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
        name="My Script",  # shown in the wallet
        description="Signs things from Python",
        url="https://example.com",  # many wallets show this host prominently
        icons=["https://example.com/icon.png"],
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
from walletconnect.types import Metadata


async def main() -> None:
    client = SignClient(metadata=Metadata(name="My Async App", url="https://example.com"))
    uri, session_task = await client.connect(chains=(1, 10))
    print_qr(uri)
    session = await client.wait_for_session(session_task)
    print(session.accounts)
    await client.close()


asyncio.run(main())
```

## App metadata (what the wallet shows)

Every session proposal carries your dApp's metadata (`proposer.metadata` in
`wc_sessionPropose`). Wallets show it on the connect sheet and on signing prompts:

- `name` and `icons` appear on most wallets' connect screens.
- The **host of `url`** is shown prominently by many wallets (MetaMask shows
  "Request from example.com"). If you point `url` at a GitHub repo, the wallet says the request
  comes from `github.com`.
- `description` is shown by some wallets on the connect screen.

Pass `metadata=` to `SignClient` or `SyncSignClient` (a `Metadata` model or an equivalent dict).
If you omit it, the library sends a deliberately brand-neutral default (`Metadata.default()`):
name `WalletConnect Python App`, url `https://example.com`, and no icon. Applications should
always set their own metadata; the client logs a hint when the default is used.
`Metadata.default(name=...)` keeps the default fields and overrides just the ones you pass.

The default url is the [RFC 2606](https://www.rfc-editor.org/rfc/rfc2606) reserved domain
`example.com` rather than an empty string because some wallets reject proposals without a valid
url (MetaMask Mobile rejects session proposals whose metadata url is not a valid URL). The client
raises `ValueError` for a non-http(s) url and logs a warning if you pass an empty one.

### Example: an Ape plugin

```python
from walletconnect import Metadata
from walletconnect.sign import SyncSignClient

client = SyncSignClient(
    metadata=Metadata(
        name="Ape",
        description="Ape Framework account via WalletConnect",
        url="https://www.apeworx.io",
        icons=["https://www.apeworx.io/assets/logo.png"],
    ),
)
```

### Verify API and domain warnings

WalletConnect's [Verify API](https://docs.reown.com/walletkit/web/verify) lets wallets check that a
request really comes from the domain in `metadata.url`. Browser dApps get attested via their page
origin; a Python process has no browser origin, so wallets will usually mark the request as
**unverified** or "unknown domain". Some wallets also warn when `url` doesn't match the domains
allowlisted for your project in Reown Cloud. This is expected for CLIs and scripts. To reduce
warnings, use a `url` on a domain you control and list it under your project's allowed domains.

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

Both show up in the wallet as "WalletConnect CLI Demo"; pass `--name`, `--url`, `--description`,
and `--icon` to change what the wallet displays.
