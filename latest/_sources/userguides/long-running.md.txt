# Long-running sessions

A WalletConnect session can stay open for weeks or months while your process mostly does
nothing. A monitoring bot is a good example: it pairs once, then waits until something needs the
user's signature. This guide covers what can happen to a session in that time and how the client
helps you handle it.

## What can happen to a session

- **The wallet changes it.** The user adds an account or a chain (`session_update`), or switches
  the active account or chain (`session_event`).
- **The wallet ends it.** The user disconnects in the wallet app (`session_delete`).
- **It expires.** Sessions last 7 days unless someone extends them (`session_expire`). The wallet
  sends nothing when this happens; the client notices on its own.
- **The phone goes quiet.** The wallet app is closed or offline. The session is still valid, but
  requests won't be answered until the user opens the app.
- **The relay connection drops.** The client reconnects, re-subscribes and fetches missed
  messages (see [Reconnects](connecting#reconnects)).

## Session events

Register handlers with `client.on(name, handler)`, or use it as a decorator. Use `"*"` to get
every event. Handlers can be plain functions or coroutines. Each one gets a
{py:class}`~walletconnect.sign.events.ClientEvent` with `name`, `topic`, `session` (a snapshot)
and `data`. Exceptions raised by a handler are logged and never reach the client. Remove a
handler with `client.off(name, handler)`.

```python
from walletconnect.sign import ClientEvent, SignClient

client = SignClient(metadata=..., state=state, on_state_change=save)


@client.on("session_delete")
async def gone(event: ClientEvent) -> None:
    print("Wallet disconnected:", event.data["message"])


client.on("*", lambda event: print(event.to_dict()))  # to_dict() has no session keys
```

| Event              | When                                                          | What the client does                                                    | `data`                          |
| ------------------ | ------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------- |
| `session_update`   | The wallet replaced the session's namespaces                  | Accepts it only from the session's controller, newest request wins      | `namespaces`                    |
| `session_event`    | The wallet sent `accountsChanged` / `chainChanged`            | Nothing. It's a UI hint; `session.namespaces` stays the source of truth | `name`, `data`, `chain_id`      |
| `session_delete`   | The wallet ended the session                                  | Drops the session; pending requests raise `SessionDeletedError`         | `code`, `message`               |
| `session_expire`   | The expiry passed (local timer, also checked on load)         | Drops the session; pending requests raise `SessionExpiredError`         | `{}`                            |
| `session_extend`   | The expiry moved forward                                      | Stores the new expiry (now + 7 days)                                    | `expiry`, `by` (wallet / dapp)  |
| `session_expiring` | Less than `expiring_warning` (24h) left with no extension yet | Emitted once per maintenance cycle                                      | `expiry`, `remaining` (seconds) |
| `relay_disconnect` | The relay WebSocket dropped                                   | Reconnects with backoff                                                 | `{}`                            |
| `relay_reconnect`  | The relay WebSocket is back                                   | Re-subscribes, fetches missed messages, prunes expired sessions         | `{}`                            |

Every change that matters for persistence (update, extend, delete, expire) also calls
`on_state_change`, so the saved state stays current.

The client also answers the wallet's own housekeeping: a wallet ping gets `true`, and a wallet
extend is applied as now + 7 days.

## Keeping a session alive

### Extending by hand

`await client.extend(topic=...)` sends `wc_sessionExtend`. The new expiry (now + 7 days) is applied
only after the wallet answers `true`, so a timeout leaves the session as it was. Most wallets
approve this without asking the user, but the app usually has to be running.

### Auto-extend

```python
client = SignClient(
    metadata=...,
    state=state,
    on_state_change=save,
    auto_extend=True,  # default: False
    auto_extend_threshold=2 * 86400,  # extend once less than 2 days remain
    expiring_warning=86400,  # warn once less than 1 day remains
    maintenance_interval=3600,  # check every hour while open
)
```

While the client is open, it checks every session once per `maintenance_interval`. When less than
`auto_extend_threshold` remains, it asks the wallet to extend. If the wallet doesn't answer
(the phone is offline), it tries again on the next cycle. Once less than `expiring_warning` is
left without a confirmed extension, it logs a warning and emits `session_expiring`. That's your
cue to ask the user to open their wallet app, or to plan for re-pairing.

Auto-extend is off by default, so a session never outlives what the user expects without you
choosing it.

### Re-pairing

An extension needs the wallet to answer. If the phone stays offline past the expiry, or the user
disconnects, the session is gone and only a new pairing (`client.connect()` and a fresh QR code)
brings it back. Plan for that path. For example, send the user a link to your pairing page when you
see `session_expiring`, `session_expire` or `session_delete`.

## Is the wallet there?

`await client.is_reachable(topic=..., timeout=10)` pings the wallet. It returns `False` on
timeout and adds the topic to `client.unreachable`. It does **not** delete the session: the phone
may simply be asleep. The mark clears as soon as the wallet answers anything. `client.ping()`
does the same but raises `WalletConnectTimeoutError` instead of returning `False`.

## Example: a Silverback watcher

A [Silverback](https://github.com/ApeWorX/silverback) bot can watch the chain and ask the user to
approve an emergency action on their phone. This sketch watches for a condition and asks the
user to send `pause()` to a contract:

```python
import os
from pathlib import Path

from silverback import SilverbackBot
from walletconnect import ClientState, Metadata
from walletconnect.exceptions import (
    SessionDeletedError,
    SessionExpiredError,
    UserRejectedError,
    WalletConnectTimeoutError,
)
from walletconnect.sign import SignClient

STATE = Path(os.environ["WC_STATE_FILE"])  # your choice of path; paired beforehand
PAUSABLE = "0x..."  # the contract to pause
PAUSE_SELECTOR = "0x8456cb59"  # pause()

bot = SilverbackBot()
client = SignClient(
    metadata=Metadata(name="Pause Guardian", description="", url="https://example.com"),
    state=ClientState.load(STATE),
    on_state_change=lambda snapshot: snapshot.save(STATE),
    auto_extend=True,
)


@client.on("session_expiring")
@client.on("session_delete")
@client.on("session_expire")
def needs_attention(event) -> None:
    bot_alert(f"WalletConnect session needs attention: {event.name}")  # your alerting


@bot.on_startup()
async def start(_state):
    await client.open()


@bot.on_shutdown()
async def stop():
    await client.close()


@bot.on_(bot.chain_manager.blocks)
async def watch(block):
    if not something_is_wrong(block):  # your detection logic
        return
    if not client.sessions:
        bot_alert("No wallet paired; re-pair to enable pausing")
        return
    if not await client.is_reachable(timeout=10):
        bot_alert("Wallet app is offline; open it to approve the pause")
        # Still send the request below: the relay queues it until the app comes back.
    session = next(iter(client.sessions.values()))
    sender = session.eip155_accounts[0].split(":")[-1]
    try:
        tx_hash = await client.request(
            "eth_sendTransaction",
            [{"from": sender, "to": PAUSABLE, "data": PAUSE_SELECTOR}],
            chain_id="eip155:1",
            timeout=15 * 60,  # give the user time to see the notification
        )
    except UserRejectedError:
        bot_alert("Pause was rejected in the wallet")
    except WalletConnectTimeoutError:
        bot_alert("Nobody approved the pause in time")
    except (SessionDeletedError, SessionExpiredError):
        bot_alert("The session ended while waiting; re-pair")
    else:
        bot_alert(f"Paused: {tx_hash}")
```

`bot_alert` and `something_is_wrong` stand in for your own code. The bot itself never holds a
key: the user signs on their phone.

## Sync client

`SyncSignClient` has the same features: `on()`, `off()`, `extend()`, `ping()`, `is_reachable()`,
`unreachable`, and the `auto_extend` options. Handlers run on the client's background event loop
thread, so keep them short and thread-safe (for example, put the event on a `queue.Queue`).
