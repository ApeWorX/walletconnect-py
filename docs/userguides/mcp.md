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
  system image viewer (see below). The `open_qr` argument of `pair` overrides it.
- `WALLETCONNECT_RELAY_URL` (optional): a different relay.
- `WALLETCONNECT_REQUEST_TIMEOUT` (optional): how long to wait for the user to approve, in
  seconds. Defaults to 300.

## Tools

```python
pair(qr="png", qr_path=None, include_uri=False, include_image=True, qr_scale=None, open_qr=None)
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

| Tool               | What it does                                                                                                                                                                  |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pair`             | Starts a pairing (asking for `eip155:1`). Writes a PNG QR file, opens it in the image viewer on a desktop, and returns `qr_opened`, `qr_path`, the image and `pairing_topic`. |
| `wait_for_pairing` | Waits up to `timeout_s` (max 300) for the wallet to approve. Returns `settled` (with the session), `failed` (with `error`) or `pending` with seconds left.                    |
| `session_status`   | Lists sessions: topic, wallet name, accounts, approved `chains`, `expiry` (unix), `expiry_iso` (UTC), `expires_in_s`, `reachable`.                                            |
| `disconnect`       | Ends a session: tells the wallet (`wc_sessionDelete`) and forgets it.                                                                                                         |
| `sign_message`     | `personal_sign` for a text message. Returns `address`, `chain_id` and `signature`.                                                                                            |
| `sign_typed_data`  | `eth_signTypedData_v4` for an EIP-712 object. Returns `address`, `chain_id`, `signature`, `added_eip712_domain` and, when needed, `warnings`.                                 |
| `send_transaction` | `eth_sendTransaction` with `to`, `value_wei`, `data` and `gas`. The wallet signs and broadcasts the transaction, and the tool returns `tx_hash` (only that).                  |

### Pairing and the QR code

`pair` writes a PNG by default (`qr="svg"` for SVG; both are drawn by `segno`, so no Pillow is
needed). The file goes to `qr_path` if you pass one, else `$WALLETCONNECT_QR_DIR`, else a new file
in the system temp directory. It's written with `0600` permissions and deleted as soon as the
pairing settles or fails, or the server stops. The PNG is about 600 pixels wide (a 4-module quiet
zone, scale picked to fit), big enough to scan straight off a laptop screen. `qr_scale` (pixels
per module, 1 to 20) overrides that, and the result reports `qr_width_px`.

The QR reaches the user in several ways at once, because what a client shows varies:

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

How (and whether) the image is rendered depends on the client, so the server tells the assistant to
present the QR itself. The server-level `instructions` (sent when the client connects) and the
`pair` description both say: after `pair`, if `qr_opened` is true, tell the user the QR code
opened in a window and to scan it with their wallet app; otherwise show the QR code prominently in
the reply (render the image, or give the path to open). Then call `wait_for_pairing`, and never
paste the `wc:` URI unless asked.

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

1. The assistant calls `session_status`, sees no sessions, then calls `pair` and shows the QR
   image (or tells the user where the file is).
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
