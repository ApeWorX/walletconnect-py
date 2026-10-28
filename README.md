# WalletConnect (Python)

A Python client for [WalletConnect v2](https://specs.walletconnect.com/2.0) (now [Reown](https://reown.com)),
so Python tools (e.g. [Ape](https://github.com/ApeWorX/ape) account plugins) can pair with mobile wallets
and relay signing requests to them.

## Status

> [!WARNING]
> This project is being revived and is **pre-alpha**. Core Sign API flow works against the public relay
> for manual testing; APIs may still change.

- **Target:** WalletConnect v2 / Reown **Sign API** (default relay `wss://relay.walletconnect.org`, `irn` JSON-RPC,
  X25519 + ChaCha20-Poly1305 envelopes, a project ID from [cloud.reown.com](https://cloud.reown.com)).
- **WalletConnect v1 is dead.** The v1 bridge servers were shut down, and this library will never
  implement the v1 protocol.
  The `0.1.0a0` release on PyPI is an empty pre-v2 skeleton and should not be used.

Progress is tracked in [#1](https://github.com/ApeWorX/walletconnect-py/issues/1).

## Dependencies

- [Python 3](https://www.python.org/downloads) version 3.10 or greater.

## Installation

### via `pip`

Once a release is available, you can install it via [`pip`](https://pypi.org/project/pip/):

```bash
pip install walletconnect
```

### via source

You can clone the repository and install for development:

```bash
git clone https://github.com/ApeWorX/walletconnect-py.git
cd walletconnect-py
uv sync --group dev
uv run prek install
```

## Quick Usage

### Terminal → mobile wallet (manual demo)

1. Create a free project at [cloud.reown.com](https://cloud.reown.com) and copy the project ID.
2. Install from this repo (with the optional QR extra):

```bash
git clone https://github.com/ApeWorX/walletconnect-py.git
cd walletconnect-py
uv sync --extra qr --group test
export WALLETCONNECT_PROJECT_ID=your_32_char_project_id
uv run --extra qr python examples/terminal_connect.py
```

Or with pip from a checkout:

```bash
pip install -e ".[qr]"
export WALLETCONNECT_PROJECT_ID=your_32_char_project_id
python examples/terminal_connect.py
```

Scan the QR with MetaMask / Rainbow / etc., approve the session, then approve `personal_sign` on the phone.

```python
from walletconnect.qr import print_qr
from walletconnect.sign import SyncSignClient
from walletconnect.types import Metadata

client = SyncSignClient(
    project_id="...",  # or set WALLETCONNECT_PROJECT_ID
    metadata=Metadata(name="my-script", description="", url="https://example.com", icons=[]),
)
session = client.connect(chains=(1,), on_uri=print_qr)
sig = client.personal_sign("hello", session.eip155_accounts[0].split(":")[-1])
client.close()
```

Lower-level helpers still work without a relay connection:

```python
from walletconnect import PairingURI
from walletconnect.qr import print_qr  # requires `pip install "walletconnect[qr]"`

uri = PairingURI.generate()
print_qr(uri)
```

`print_qr` draws for a dark terminal background by default; pass `invert=False` for light
backgrounds. Live relay tests skip unless `WALLETCONNECT_PROJECT_ID` is set.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Comments, questions, criticisms and pull requests are welcomed.

## License

This project is licensed under the [Apache 2.0](LICENSE).
