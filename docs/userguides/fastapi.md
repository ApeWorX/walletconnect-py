# FastAPI example app

`examples/fastapi_app/` is a small HTTP service built on the async
{py:class}`~walletconnect.sign.client.SignClient`. A backend, bot or web UI can pair phones over
HTTP and ask them for signatures. Use it as a starting point for your own service.

## Run it

```bash
export WALLETCONNECT_PROJECT_ID=your_32_char_id    # https://dashboard.walletconnect.com
export WALLETCONNECT_STATE_FILE=./wc-state.json    # optional: keep sessions across restarts
uv run --group examples --extra qr uvicorn examples.fastapi_app.app:app --reload
```

The `examples` dependency group installs `fastapi` and `uvicorn`. The SDK itself does not depend
on them. The `qr` extra is only needed for `?qr=svg`.

## Endpoints

| Method and path                 | What it does                                                                       |
| ------------------------------- | ---------------------------------------------------------------------------------- |
| `POST /pair[?qr=svg]`           | Starts a pairing and returns `uri`, `pairing_topic`, and optionally `qr_svg`.      |
| `GET /pairings/{pairing_topic}` | Shows the pairing status: `pending`, `settled` (with `session_topic`) or `failed`. |
| `GET /sessions`                 | Lists active sessions with their topic, wallet name, accounts and expiry.          |
| `POST /sessions/{topic}/sign`   | Sends `personal_sign` or `eth_signTypedData_v4` and waits for the user.            |
| `DELETE /sessions/{topic}`      | Disconnects the session (`wc_sessionDelete`).                                      |

`POST /pair` returns right away. Show the URI or QR code to the user, and the session settles in the
background. Poll `GET /pairings/{pairing_topic}` (or `GET /sessions`) to see when it's ready.

```bash
curl -X POST 'localhost:8000/pair?qr=svg'
curl localhost:8000/pairings/<pairing_topic>
curl -X POST localhost:8000/sessions/<topic>/sign \
     -H 'content-type: application/json' -d '{"message": "hello"}'
curl -X POST localhost:8000/sessions/<topic>/sign -H 'content-type: application/json' \
     -d '{"method": "eth_signTypedData_v4", "typed_data": {...}}'
```

The sign endpoint takes these fields:

- `method`: `personal_sign` (the default) or `eth_signTypedData_v4`.
- `message`: the text to sign, for `personal_sign`.
- `typed_data`: the EIP-712 JSON, for `eth_signTypedData_v4`.
- `address`: defaults to the session's first account.
- `chain_id`: defaults to that account's chain.

Errors map to HTTP status codes:

- 404: unknown session.
- 410: the session expired.
- 409: the wallet rejected the request.
- 504: the user didn't respond in time.

## How it's wired

- **Lifespan:** a single `SignClient` is created on startup. `await client.open()` resumes any
  sessions restored from the state file, and `client.close()` runs on shutdown.
- **State:** the library never chooses a path. The example reads `WALLETCONNECT_STATE_FILE` and
  passes `ClientState.load(path)` into the client. Its `on_state_change` callback calls
  `snapshot.save(path)`, which does an atomic write with `0600` permissions. To store state
  somewhere else (a database, a secrets manager, ...), swap out those two calls.
- **Testing:** `create_app(client_factory=...)` lets you inject a client. The repository's smoke
  test (`tests/test_example_fastapi.py`) drives the whole flow with `TestClient` against the
  in-process fake relay.

```{warning}
The example has **no authentication**. Anyone who can reach it can ask the paired wallets to
sign. Put it behind your own auth before exposing it. The state file contains secret keys, so
keep it out of version control.
```
