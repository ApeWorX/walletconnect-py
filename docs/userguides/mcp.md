# MCP server example

`examples/mcp_server/` is a small [Model Context Protocol](https://modelcontextprotocol.io) server.
It lets an AI assistant pair a mobile wallet and ask it to sign messages, typed data and
transactions. The server never holds a private key: every request shows up in the user's wallet
app, and nothing happens until the user approves it there.

It uses the official [`mcp` Python SDK](https://github.com/modelcontextprotocol/python-sdk)
(`MCPServer`, which was called `FastMCP` before `mcp` 2.0) over stdio. The SDK itself has no MCP
dependency. `mcp` is only in the `examples` dependency group.

```{note}
This is an example. The production MCP integration is planned as a feature of the
`ape-walletconnect` plugin. Then any Ape-based MCP
server (for example a Uniswap MCP) can reuse the same wallet tools instead of copying this file.
```

## Run it

```bash
export WALLETCONNECT_PROJECT_ID=your_32_char_id   # from https://dashboard.walletconnect.com
export WALLETCONNECT_STATE_FILE=./wc-mcp.json     # optional: keep sessions across restarts
uv run --group examples --extra qr python -m examples.mcp_server
```

The server speaks MCP on stdin/stdout, and its logs go to stderr. Most MCP clients start it for you.
For example, in a client that uses the common `mcpServers` JSON config:

```json
{
  "mcpServers": {
    "walletconnect": {
      "command": "/home/you/.local/bin/uv",
      "args": [
        "run", "--directory", "/path/to/walletconnect-py",
        "--group", "examples", "--extra", "qr",
        "python", "-m", "examples.mcp_server"
      ],
      "env": {
        "WALLETCONNECT_PROJECT_ID": "your_32_char_id",
        "WALLETCONNECT_STATE_FILE": "/home/you/.local/share/wc-mcp.json"
      }
    }
  }
}
```

### Claude Desktop

On Linux the config file is `~/.config/Claude/claude_desktop_config.json` (also reachable from
Claude's Settings, under Developer, then Edit Config). Use the **absolute path** to `uv` (whatever
`which uv` prints) as `command`: GUI apps often don't inherit your shell's `PATH`, and a bare
`"uv"` fails with `spawn uv ENOENT`. Fully quit and reopen Claude Desktop after editing.

Environment variables:

- `WALLETCONNECT_PROJECT_ID` (required): your WalletConnect project ID.
- `WALLETCONNECT_STATE_FILE` (optional): an explicit path. The server loads the client state from
  it on start and saves it on every change, with `0600` permissions. Without it, sessions only
  last as long as the process.
- `WALLETCONNECT_QR_DIR` (optional): the folder where `pair` writes QR images. Without it, and
  without a `qr_path` argument, they go to a new file in the system temp directory.
- `WALLETCONNECT_OPEN_QR` (optional): `1` or `0` to always or never open the pairing QR in the
  system image viewer (see below). The `open_qr` argument of `pair` overrides it. With the default
  `qr="svg_inline"`, only `1` (or `open_qr=true`) opens a viewer.
- `WALLETCONNECT_RELAY_URL` (optional): a different relay.
- `WALLETCONNECT_REQUEST_TIMEOUT` (optional): how long to wait for the user to approve, in
  seconds. Defaults to 300.

## Tools

```python
pair(
    qr="svg_inline",  # or "png", "svg", "text", "none"
    qr_path=None,
    include_uri=False,
    include_image=True,
    qr_scale=None,
    open_qr=None,
    deep_link=None,  # "metamask", "rainbow", "trust", "uniswap", "zerion", "generic"
)
wait_for_pairing(pairing_topic, timeout_s=60)
session_status(pairing_topic=None, check_reachable=False)
disconnect(topic=None)
sign_message(message, address=None, topic=None, chain_id=None, timeout_s=None)
sign_typed_data(typed_data, address=None, topic=None, chain_id=None, timeout_s=None)
send_transaction(
    to,
    value_wei="0",
    data=None,
    gas=None,
    from_address=None,
    topic=None,
    chain_id=None,
    timeout_s=None,
)
```

| Tool               | What it does                                                                                                                                                                                                                          |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pair`             | Starts a pairing (asking for `eip155:1`). Returns the QR code as inline SVG markup for the assistant to render (default), or writes a PNG file. Returns `pairing_topic` and `seconds_remaining`, and a wallet `deep_link` on request. |
| `wait_for_pairing` | Waits up to `timeout_s` (max 300) for the wallet to approve. Returns `settled` (with the session), `failed` (with `error`) or `pending` with seconds left.                                                                            |
| `session_status`   | Lists sessions: topic, wallet name, accounts, approved `chains`, `expiry` (unix), `expiry_iso` (UTC), `expires_in_s`, `reachable`.                                                                                                    |
| `disconnect`       | Ends a session: tells the wallet (`wc_sessionDelete`) and forgets it.                                                                                                                                                                 |
| `sign_message`     | `personal_sign` for a text message. Returns `address`, `chain_id` and `signature`.                                                                                                                                                    |
| `sign_typed_data`  | `eth_signTypedData_v4` for an EIP-712 object. Returns `address`, `chain_id`, `signature`, `added_eip712_domain` and, when needed, `warnings`.                                                                                         |
| `send_transaction` | `eth_sendTransaction` with `to`, `value_wei`, `data` and `gas`. The wallet signs and broadcasts the transaction, and the tool returns `tx_hash` (only that).                                                                          |

### Pairing and the QR code

By default (`qr="svg_inline"`) `pair` returns the QR code as SVG markup in the result text, between
`-----BEGIN QR SVG-----` and `-----END QR SVG-----`, for the assistant to copy verbatim into its
inline widget or artifact tool. Nothing is written to disk, and the `wc:` URI never appears as text,
only as the QR pattern (`include_uri=true` is refused in this mode). The markup is built to be easy
to reproduce exactly and to scan:

- Error correction level L, exactly (no automatic boost to a higher level), and never a Micro QR.
  L needs the fewest modules, and a QR on a screen doesn't get scratched or dirty. A typical
  pairing URI (about 190 characters) is a version 8 code: 57×57 modules with the quiet zone,
  instead of 65×65 at level M. The PNG and SVG files use level L too, as does `qr="text"`.
- All dark modules are one `<path>`: a stroke along each run of dark modules in a row, with
  relative moves and integer coordinates. That's about 4.0 KB for a typical pairing URI (it was
  5.3 KB at level M). The result reports `qr_svg_bytes` and `qr_modules`, and repeats the markup as
  `qr_svg`.
- A 4-module white quiet zone and an explicit white background `<rect>`, so it scans in dark mode.
- A square `viewBox` with `width="100%"` and no fixed height, and `shape-rendering="crispEdges"`,
  so it scales up sharply.
- No XML declaration, `<title>` or `<desc>`.

The result looks like this (the markup is shortened here):

```text
Render this SVG QR code exactly as given (everything between the markers) with your inline widget/artifact tool at a large size (3960 bytes):
-----BEGIN QR SVG-----
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 57 57" width="100%" shape-rendering="crispEdges"><rect width="57" height="57" fill="#fff"/><path stroke="#000" d="M4 4.5h7m2 0h1…"/></svg>
-----END QR SVG-----
pairing_topic: 67c7f3aa…
expires: 2026-11-03T01:47:17Z (300s)
After pair, render the returned SVG exactly as given with your inline widget/artifact tool at a large size, tell the user to scan it with their wallet, then call wait_for_pairing. If you cannot render widgets, call pair again with qr="text" or qr="png".
```

In this mode the server doesn't open an image viewer unless asked (`open_qr=true` or
`WALLETCONNECT_OPEN_QR=1`); then it also writes a PNG file and opens it.

If the client can't render widgets, use one of the other modes. `qr="png"` writes a PNG file
(`qr="svg"` an SVG file; both are drawn by `segno`, so no Pillow is needed). The file goes to `qr_path` if you pass one, else `$WALLETCONNECT_QR_DIR`, else a new file
in the system temp directory. It's written with `0600` permissions and deleted as soon as the
pairing settles or fails, or the server stops. The PNG is about 600 pixels wide (a 4-module quiet
zone, scale picked to fit), big enough to scan straight off a laptop screen. `qr_scale` (pixels
per module, 1 to 20) overrides that, and the result reports `qr_width_px`.

With `qr="png"` the QR reaches the user in several ways at once, because what a client shows
varies:

- **A viewer window.** When the server runs in a desktop session, `pair` opens the PNG in the
  system image viewer: `xdg-open` on Linux when `DISPLAY` or `WAYLAND_DISPLAY` is set, `open` on
  macOS, `os.startfile` on Windows. The opener runs in the background with its input and output
  sent to `/dev/null`, because the server's stdout is the MCP channel. `open_qr=true/false` (or
  `WALLETCONNECT_OPEN_QR=1/0`) forces it either way. The result reports `qr_opened` and
  `qr_open_reason`, and when it worked the text says the QR was opened in an image viewer window.
  The file stays until the pairing settles, fails or expires, so the viewer can still load it.
  This is the most reliable path in Claude Desktop, which keeps tool-result images in a collapsed
  panel.
- **The file.** The result's text is short: a line `Show this QR code to the user so they can scan it: <path>`, the `file://` URI, the `pairing_topic` and the expiry. `xdg-open <path>` (Linux) or
  `open <path>` (macOS) shows it full size.
- **An inline image.** With `include_image=true` (the default) the PNG is attached as MCP image
  content. Claude Desktop renders it, but inside the collapsible `pair` tool-result panel, where it
  can be easy to miss.
- **A resource.** While the pairing is pending, the PNG is also the MCP resource
  `walletconnect://pairing/<pairing_topic>/qr.png`, and the result links to it (`resource_link`),
  so clients that support resources can attach it.

How (and whether) a QR is rendered depends on the client, so the server tells the assistant to
present the QR itself. The server-level `instructions` (sent when the client connects) and the
`pair` description both say: "After pair, render the returned SVG exactly as given with your inline
widget/artifact tool at a large size, tell the user to scan it with their wallet, then call
wait_for_pairing. If you cannot render widgets, call pair again with qr="text" or qr="png"." For
`qr="png"`, if `qr_opened` is true the assistant says the QR opened in a window; otherwise it shows
the image or gives the path. It never pastes the `wc:` URI unless asked.

`qr="text"` returns the QR inline as text instead, in the most compact form: Unicode half blocks
(two module rows per line), error correction level L, never a Micro QR, and a 2-module quiet zone
(drawn for a dark background). `qr="none"` makes no QR at all. The QR needs the `qr` extra
(`pip install "walletconnect[qr]"`).

After showing the QR, call `wait_for_pairing(pairing_topic)`. It returns as soon as the wallet
approves. If it's still `pending` when `timeout_s` runs out, it says how many seconds the pairing
has left, and the assistant can call it again. A pairing expires after 5 minutes; after that, call
`pair` again. (`pair` itself doesn't wait, because the assistant can only show the QR once the tool
has returned.)

```{warning}
The `wc:` URI contains the pairing's symmetric key (`symKey`). It's a short-lived secret: anyone
who sees it before the wallet scans it could answer the pairing. It's useless once the pairing has
settled or expired (5 minutes), but anything in a result stays in the chat transcript. So `pair`
leaves it out by default; pass `include_uri=true` only when the user wants to paste it into their
wallet. The QR encodes the same URI, which is why its file is private and short-lived.
```

### Wallet deep links

If the user is chatting on the same phone as their wallet, they can't scan the QR. For that case (or
when scanning fails), `pair(deep_link="<wallet>")` also returns a link that opens the wallet with
the pairing request. It's off by default, and the server instructions tell the assistant to offer
it only when the user is on the same phone or can't scan. It works with every `qr` mode, including
the default `svg_inline` (and with `qr="none"`, the link alone). The result gets `deep_link`,
`deep_link_wallet` and `deep_link_warning`, and the text has a line with the link.

```{warning}
The deep link contains the URL-encoded `wc:` URI, symmetric key included: it's the same short-lived
secret as the URI (see above). Only ask for it when the user needs it, and only give it to them.
```

Each link is the wallet's WalletConnect link with `?uri=` followed by the `wc:` URI, URL-encoded
like JavaScript's `encodeURIComponent`. This is the format in WalletConnect's
[mobile linking docs](https://docs.walletconnect.com/wallets/ios/mobile-linking). Only wallets whose
link we could check against the wallet's own docs or code, and against the wallet's entry in the
[WalletConnect Explorer](https://explorer.walletconnect.com/) registry, are included. The https
universal links open the app if it's installed and a web page otherwise.

| `deep_link`              | Link                                        | Sources                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------ | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `metamask`               | `https://metamask.app.link/wc?uri=<uri>`    | Registry (`mobile.universal`: `https://metamask.app.link`); MetaMask Mobile's [deep link actions](https://github.com/MetaMask/metamask-mobile/blob/main/app/constants/deeplinks.ts) (`wc`) and [universal link host](https://github.com/MetaMask/metamask-mobile/blob/main/app/core/AppConstants.ts)                                                                                 |
| `rainbow`                | `https://rnbwapp.com/wc?uri=<uri>`          | Registry (`mobile.universal`: `https://rnbwapp.com`); Rainbow's [RainbowKit connector](https://github.com/rainbow-me/rainbowkit/blob/main/packages/rainbowkit/src/wallets/walletConnectors/rainbowWallet/rainbowWallet.ts)                                                                                                                                                           |
| `trust`                  | `https://link.trustwallet.com/wc?uri=<uri>` | Registry (`mobile.universal`: `https://link.trustwallet.com`); Trust Wallet's [deep linking docs](https://developer.trustwallet.com/developer/develop-for-trust/deeplinking)                                                                                                                                                                                                         |
| `uniswap`                | `https://uniswap.org/app/wc?uri=<uri>`      | Registry (`mobile.universal`: `https://uniswap.org/app`); the Uniswap Wallet's [deep link constants](https://github.com/Uniswap/interface/blob/main/apps/mobile/src/features/deepLinking/constants.ts) (`UNISWAP_WALLETCONNECT_URL`)                                                                                                                                                 |
| `zerion`                 | `zerion://wc?uri=<uri>`                     | Registry (`mobile.native`: `zerion://`); the [RainbowKit Zerion connector](https://github.com/rainbow-me/rainbowkit/blob/main/packages/rainbowkit/src/wallets/walletConnectors/zerionWallet/zerionWallet.ts). Zerion's registry universal link (`https://wallet.zerion.io/wc`) has no documented `?uri=` form, so this is the app's URL scheme: it only works with Zerion installed. |
| `generic` or `universal` | the `wc:` URI itself                        | WalletConnect's [Android mobile linking docs](https://docs.walletconnect.com/wallets/android/mobile-linking): wallets register the `wc:` scheme, so Android offers a choice of installed wallets. iOS doesn't route `wc:` links reliably; name the wallet there.                                                                                                                     |

Other wallets (Coinbase Wallet, Phantom, Ledger Live and so on) aren't included. An unknown name
is refused with `invalid_argument:` and the list of supported names, before any pairing starts.

### Choosing the session, account and chain

- **Session:** `topic` is optional when exactly one session is open. With several sessions and no
  `topic`, the tool fails with `ambiguous_session:` and lists each topic with its wallet name.
  `disconnect` follows the same rule.
- **Account:** `address` / `from_address` defaults to the session's first `eip155` account, and
  must be one of the session's accounts.
- **Chain:** `chain_id` is a CAIP-2 id such as `"eip155:1"`. A bare number (`"1"`, `1` or `"0x1"`)
  is accepted too and normalized. It defaults to the chain of the chosen account and must be one of
  the session's approved chains (see `chains` in `session_status`), otherwise the tool fails with
  `unapproved_chain:`. To use another chain, pair again.

### Typed data

`typed_data` is an object with `types`, `primaryType`, `domain` and `message`. If
`types.EIP712Domain` is missing, the tool **adds it** from the fields present in `domain`, in the
standard order (`name`, `version`, `chainId`, `verifyingContract`, `salt`), and returns
`added_eip712_domain: true`. If you include `EIP712Domain` yourself, it's passed through unchanged
(do that for non-standard domain fields). The request is sent on `chain_id` as above. The tool
doesn't change `domain.chainId`, but when it differs from the request chain the result includes a
`warnings` entry: a signature is only valid where the verifying contract's chain matches
`domain.chainId`.

### Amounts

`value_wei` and `gas` take an integer, a decimal string (`"1000000000000000000"`) or a `0x` hex
string. Prefer strings for large values, because some JSON clients lose precision above 2^53.
Negative, fractional and out-of-range values are rejected with `invalid_argument:`.

### Blocking, timeouts and errors

The signing tools **block** until the user approves or rejects in the wallet, or until `timeout_s`
passes (default: `WALLETCONNECT_REQUEST_TIMEOUT`, 300 seconds). A WalletConnect request can't be
withdrawn: if the tool times out, or the MCP client cancels the call, the prompt may still be
showing in the wallet, so the user should reject it there if it's no longer wanted. Failures come
back as tool errors that start with a category the assistant can act on:

| Prefix                                                                           | Meaning                                                                                                                                    |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `rejected:`                                                                      | The user rejected the request in the wallet.                                                                                               |
| `timeout:`                                                                       | No answer within `timeout_s`. If the last ping failed too, it says the phone may be offline; check `session_status(check_reachable=true)`. |
| `session_ended:`                                                                 | No session, or the session was deleted by the wallet or expired. Call `pair` again.                                                        |
| `ambiguous_session:`                                                             | Several sessions are open and no `topic` was given. The error lists the topics.                                                            |
| `unapproved_chain:`                                                              | `chain_id` isn't approved in the session.                                                                                                  |
| `invalid_chain:`, `invalid_account:`, `invalid_argument:`, `invalid_typed_data:` | A bad argument.                                                                                                                            |
| `wallet_error:`                                                                  | The wallet returned another WalletConnect error code (included in the message).                                                            |
| `relay_unavailable:`                                                             | The relay connection was lost and not restored in time.                                                                                    |

With `check_reachable=true`, `session_status` pings each wallet first (up to 10 seconds per offline
wallet), and `reachable` shows the result.

A typical conversation:

1. The assistant calls `session_status`, sees no sessions, then calls `pair` and renders the
   returned SVG QR code in the chat (or, with `qr="png"`, shows the image or the file path).
2. The user scans it, and the assistant calls `wait_for_pairing(pairing_topic=...)` until it's
   `settled`.
3. The assistant calls `send_transaction` and tells the user to check their phone. The user
   approves, and the tool returns the transaction hash.

## Planned for the Ape plugin

This example deliberately stops at forwarding requests to the wallet. These belong in the
`ape-walletconnect` plugin, which has Ape's ABI decoding, networks and providers to do them well:

- **Pre-flight summaries:** decode what a request does (spender, amount, deadline, chain) and flag
  risky patterns such as `Permit` / Permit2 approvals, `setApprovalForAll`, max-uint256 amounts,
  long deadlines and unknown spenders, then ask for confirmation in chat as well as in the wallet.
- **Guardrails:** optional allowlists of spenders and recipients, spend caps, and a mainnet/testnet
  switch.
- **Signer recovery:** return the EIP-712 digest or domain separator and the signer recovered
  locally, so the result can be checked against what was sent.
- **Transaction follow-up:** receipts (`wait_for_receipt`), gas estimates, simulation / dry runs,
  and nonce visibility.

## Testing

`tests/test_example_mcp.py` connects an in-process `mcp.Client` to `create_server(...)`, with a
client that talks to the in-process fake relay and a fake wallet. It calls every tool, including
QR file output, waiting for a pairing, chain validation, a rejected and a timed-out request,
several sessions, and `disconnect`. `create_server(client_factory=...)` is the hook for injecting
your own client.

```{warning}
Anything that can talk to this server can ask the paired wallet for signatures and
transactions. The user still approves each one on their phone, but only connect clients you
trust. The state file contains secret keys, so keep it out of version control.
```
