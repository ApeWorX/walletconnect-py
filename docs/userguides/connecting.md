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

## Sessions and state (stateless by design)

The client never reads or writes files and never picks a storage location. Everything it needs
to resume lives in memory as a `ClientState` pydantic model:

- `relay_key`: the Ed25519 seed behind the relay identity (`did:key` client ID), so the relay
  mailbox and subscriptions follow you across restarts.
- `sessions`: every settled session keyed by topic, including each session's symmetric key.

Take a snapshot with `export_state()`, store it wherever suits your app, and pass it back with
`state=`:

```python
from walletconnect.sign import ClientState, SignClient

client = SignClient(metadata=...)
uri, task = await client.connect()
session = await client.wait_for_session(task)

blob = client.export_state().model_dump_json()  # -> your database, S3, a file, ...
await client.close()

# Later, possibly in another process:
client = SignClient(state=ClientState.model_validate_json(blob))
await client.open()  # reconnects, resubscribes session topics, fetches queued messages
await client.personal_sign("still here", address)
```

On restore the client reuses the relay key, restores each session's key, drops sessions whose
`expiry` has passed, resubscribes the remaining session topics, and fetches messages the relay
queued while you were offline (for example a wallet-side account change or disconnect).

If you just want a file, `ClientState.save(path)` writes it atomically with `0600` permissions
and `ClientState.load(path)` reads it back. You must always pass the path explicitly.

To save on every change instead of on exit, pass `on_state_change=`. The client calls it with a
fresh snapshot whenever a session settles, updates, extends, expires or is deleted:

```python
client = SignClient(state=ClientState.load(path), on_state_change=lambda s: s.save(path))
```

```{warning}
`ClientState` contains **secrets**: the relay key and every session's symmetric key. Anyone with
the state can send requests to the wallet as your app (the user still has to approve them on the
phone). Store it like a password: encrypted at rest, with tight permissions, and never in logs or
source control. The models hide keys from `repr()`, but `model_dump_json()` includes them.
```

### Several sessions at once

A client can hold several pairings and sessions at once. Each `connect()` returns its own URI and
task. `client.sessions` maps topic to `Session`. When exactly one session is active, `request()`,
`personal_sign()`, `ping()` and `disconnect()` use it automatically. Otherwise, pass `topic=`.
They raise `NoActiveSessionError` when there's no session or the choice is ambiguous, and
`SessionExpiredError` when the session has expired.

`disconnect()` ends a session (`wc_sessionDelete`) but leaves the relay connection open. Call
`close()` to shut the connection down.

## Relay

The default relay is `wss://relay.walletconnect.org`. Pass `relay_url=` to use another one.

### Reconnects

Long-running processes will see the relay WebSocket drop now and then (deploys, load
balancing, network blips, an expired auth token). The client reconnects on its own:

- **Backoff**: after an unexpected drop it waits 0.25–0.5s, then doubles up to a 30s cap,
  with random jitter so many clients don't reconnect in lockstep. Close codes 1001 and 4010
  ("going away" / rebalancing) reconnect at once. A connection that dies within 10s of
  opening does not reset the backoff, so a flapping relay is not hammered.
- **Fresh auth**: every attempt signs a new relay JWT with the same client key (`client_id` does
  not change).
- **Resubscribe + catch up**: every subscribed topic (sessions and open pairings) is
  re-subscribed, then `irn_fetchMessages` pulls anything published while offline. Duplicates
  are dropped.
- **In-flight requests**:
  - Relay calls (publish, subscribe, and so on) made while disconnected, or whose reply was
    lost in the drop, are re-sent after reconnecting. The relay and the wallet treat a repeated
    publish as the same message.
  - If the relay is not back within the relay request timeout (30s or more), the call raises
    {py:class}`~walletconnect.exceptions.RelayDisconnectedError`.
  - A wallet request that was already sent (for example `personal_sign` waiting for the user)
    keeps waiting. The wallet's answer is queued on the relay and fetched after reconnecting.
- **Giving up**: if the relay rejects the handshake with HTTP 400/401/403 (for example, a bad
  project ID), it stops retrying. Later calls raise `RelayDisconnectedError` until you call
  `open()` again.

Pass `auto_reconnect=False` to turn this off. Calls then raise `RelayDisconnectedError` as
soon as the socket drops.

## Long-running sessions

Sessions last 7 days unless extended, and the wallet can update or end them at any time. See
[Long-running sessions](long-running) for session events (`client.on(...)`), `extend()` and
`auto_extend=True`, reachability checks, and re-pairing.

## Examples

The repository ships these runnable demos:

- `examples/terminal_connect.py` uses `SyncSignClient`.
- `examples/async_cli.py` is an async [Cyclopts](https://cyclopts.readthedocs.io) CLI (install the
  `cli` extra).
- `examples/fastapi_app/` is an HTTP service for pairing and signing (see the [FastAPI example](fastapi)).

The two CLI demos show up in the wallet as "WalletConnect CLI Demo"; pass `--name`, `--url`, `--description`,
and `--icon` to change what the wallet displays.
