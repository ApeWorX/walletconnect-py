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
uv run --group examples python -m examples.mcp_server
```

The server speaks MCP on stdin/stdout, and its logs go to stderr. Most MCP clients start it for you.
For example, in a client that uses the common `mcpServers` JSON config:

```json
{
  "mcpServers": {
    "walletconnect": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/walletconnect-py", "--group", "examples",
        "python", "-m", "examples.mcp_server"
      ],
      "env": {
        "WALLETCONNECT_PROJECT_ID": "your_32_char_id",
        "WALLETCONNECT_STATE_FILE": "/path/to/wc-mcp.json"
      }
    }
  }
}
```

Environment variables:

- `WALLETCONNECT_PROJECT_ID` (required): your WalletConnect project ID.
- `WALLETCONNECT_STATE_FILE` (optional): an explicit path. The server loads the client state from
  it on start and saves it on every change, with `0600` permissions. Without it, sessions only
  last as long as the process.
- `WALLETCONNECT_RELAY_URL` (optional): a different relay.
- `WALLETCONNECT_REQUEST_TIMEOUT` (optional): how long to wait for the user to approve, in
  seconds. Defaults to 300.

## Tools

| Tool               | What it does                                                                                                                                     |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `pair`             | Starts a pairing. Returns the `wc:` URI, a `pairing_topic` and a QR code: `qr="text"` (Unicode blocks), `"svg"`, or `"none"`.                    |
| `session_status`   | Lists sessions (topic, wallet name, accounts, expiry, reachable). Pass `pairing_topic` to see if a pairing settled.                              |
| `sign_message`     | `personal_sign` for a text message. Returns `address` and `signature`.                                                                           |
| `sign_typed_data`  | `eth_signTypedData_v4` for an EIP-712 object. Returns `address` and `signature`.                                                                 |
| `send_transaction` | `eth_sendTransaction` with `to`, `value_wei`, `data` and `gas`. The wallet signs and broadcasts the transaction, and the tool returns `tx_hash`. |

The signing tools pick the only open session and its first account unless you pass `topic`,
`address` / `from_address`, or `chain_id` (CAIP-2, such as `eip155:1`). A rejection, a timeout, or a
session that has ended comes back as a tool error the assistant can read, so it can tell the user
or call `pair` again. With `check_reachable=true`, `session_status` pings each wallet first.

The QR code needs the `qr` extra (`pip install "walletconnect[qr]"`). Without it, `qr` is
`null` and the URI still works: paste it into the wallet.

A typical conversation:

1. The assistant calls `session_status`, sees no sessions, then calls `pair` and shows the QR
   code.
2. The user scans it, and the assistant polls `session_status(pairing_topic=...)` until it's
   `settled`.
3. The assistant calls `send_transaction` and tells the user to check their phone. The user
   approves, and the tool returns the transaction hash.

## Testing

`tests/test_example_mcp.py` connects an in-process `mcp.Client` to `create_server(...)`, with a
client that talks to the in-process fake relay and a fake wallet. It calls every tool, including
a rejected request. `create_server(client_factory=...)` is the hook for injecting your own
client.

```{warning}
Anything that can talk to this server can ask the paired wallet for signatures and
transactions. The user still approves each one on their phone, but only connect clients you
trust. The state file contains secret keys, so keep it out of version control.
```
