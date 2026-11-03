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

Then open <http://localhost:8000/> for the demo page, or use the JSON API below.

The `examples` dependency group installs `fastapi` and `uvicorn`. The SDK itself does not depend
on them. The `qr` extra is only needed for `?qr=svg`.

## Demo page

`GET /` serves a small page called "WalletConnect Python Demo". It's one self-contained HTML
file (`examples/fastapi_app/index.html`) with inline CSS and vanilla JavaScript: no build step and
no CDN. On the page you can:

- Press **Pair** to call `POST /pair?qr=svg`. The page shows the QR code and a copyable URI, then
  polls `GET /pairings/{pairing_topic}` until the wallet approves.
- See the sessions from `GET /sessions`, each with a **Disconnect** button
  (`DELETE /sessions/{topic}`).
- Type a message and press **Sign** next to a session. This calls `POST /sessions/{topic}/sign`
  with `personal_sign` and shows the signature or the error.

The page only calls the JSON API. Values from the API are inserted as text, never as HTML. The QR
SVG is shown through an `<img>` data URL, so any markup in it can't run script. The response also
carries a strict `Content-Security-Policy` (no external sources, `connect-src 'self'`).

## Endpoints

| Method and path                 | What it does                                                                       |
| ------------------------------- | ---------------------------------------------------------------------------------- |
| `GET /`                         | The demo page (HTML).                                                              |
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
