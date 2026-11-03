# MCP server example

`examples/mcp_server/` is a small, readable [Model Context Protocol](https://modelcontextprotocol.io)
server: a show-and-tell demo of the SDK.
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

`WALLETCONNECT_PROJECT_ID` is required. `WALLETCONNECT_STATE_FILE` is optional: the server loads
the client state from it on start and saves it on every change (with `0600` permissions), so
sessions survive restarts.

## Tools

```python
pair(qr="svg_inline")  # or qr="text"
wait_for_pairing(pairing_topic, timeout_s=60)
session_status()
disconnect(topic=None)
sign_message(message, topic=None, chain_id=None)
sign_typed_data(typed_data, topic=None, chain_id=None)
send_transaction(to, value_wei="0", data=None, topic=None, chain_id=None)
```

- `pair` starts a pairing (asking for `eip155:1`) and returns its QR code (see below), the
  `pairing_topic` and `expires_at`.
- `wait_for_pairing` waits up to `timeout_s` (max 300) and returns `settled` (with the session),
  `failed` (with `error`), `pending` (call it again) or `unknown`.
- `session_status` lists the sessions: topic, wallet name, accounts, approved `chains` and
  `expiry_iso`.
- `disconnect` ends a session and tells the wallet.
- `sign_message` (`personal_sign`), `sign_typed_data` (`eth_signTypedData_v4`) and
  `send_transaction` (`eth_sendTransaction`; the wallet signs and broadcasts, and the tool returns
  `tx_hash`) block until the user approves or rejects on their phone.

The signing tools use the session's first account. `topic` is only needed when several sessions
are open. `chain_id` is CAIP-2 (`"eip155:1"`) or a number (`1`, `"0x1"`), and must be approved in
the session; it defaults to the account's chain. `sign_typed_data` adds `types.EIP712Domain` from
`domain` when it's missing. `value_wei` takes an integer, a decimal string (best for big values)
or `0x` hex.

Errors start with a category the assistant can act on: `rejected:`, `timeout:` (the prompt may
still be showing in the wallet; it can't be withdrawn), `session_ended:`, `ambiguous_session:`,
`unapproved_chain:`, `invalid_chain:`, `invalid_argument:`, `invalid_typed_data:` or
`wallet_error:`.

## Pairing and the QR code

By default `pair` returns the QR code as SVG markup in the result text, between
`-----BEGIN QR SVG-----` and `-----END QR SVG-----` (and as `qr_svg`). The server instructions and
the `pair` description tell the assistant: "After pair, render the returned SVG exactly as given
with your inline widget/artifact tool at a large size, tell the user to scan it with their wallet,
then call wait_for_pairing. If you cannot render widgets, call pair again with qr="text"."

`qr="text"` is the fallback: a compact Unicode QR (half blocks, two module rows per line) in
`qr_text`. The QR needs the `qr` extra (`pip install "walletconnect[qr]"`).

A typical conversation: the assistant calls `session_status`, sees no sessions, calls `pair` and
renders the QR. The user scans it, the assistant calls `wait_for_pairing` until it's `settled`,
then calls `send_transaction` and tells the user to check their phone.

### Why the QR is built this way

- **Inline SVG, not an image.** Clients such as Claude Desktop show tool-result images in a
  collapsed panel, where users miss them. SVG markup the assistant renders with its own widget or
  artifact tool appears right in the chat.
- **Small enough to copy exactly.** The assistant has to reproduce the markup character for
  character. Every dark module is in a single `<path>`: a stroke along each run of dark modules in
  a row, with relative moves. Error correction is level L exactly (no automatic boost), the
  fewest modules that scan: a typical pairing URI is a 57×57 grid in about 4 KB, against 65×65
  and 5.3 KB at level M. A screen doesn't get scratched, so L's lower damage tolerance is fine.
- **Scannable in dark mode.** A white background `<rect>` and a 4-module white quiet zone, so the
  code stays dark-on-light even in a dark chat theme.
- **Sharp at any size.** A square `viewBox`, `width="100%"` with no fixed height, and
  `shape-rendering="crispEdges"`.
- **Plain text between markers.** The text result isn't JSON, so the markup has no escaped quotes,
  and the markers show exactly what to copy.
- **The secret stays out of the transcript.** The `wc:` URI holds the pairing's symmetric key, and
  anything in a result stays in the chat. `pair` never returns the URI as text, only as the QR
  pattern. No XML declaration, `<title>` or `<desc>`.

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
client on the in-process fake relay and a fake wallet. It checks the QR markup module by module,
then pairs and calls every tool, including a rejected and a timed-out request and several
sessions. `create_server(client_factory=...)` is the hook for injecting your own client.

```{warning}
Anything that can talk to this server can ask the paired wallet for signatures and
transactions. The user still approves each one on their phone, but only connect clients you
trust. The state file contains secret keys, so keep it out of version control.
```
