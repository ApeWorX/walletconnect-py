# WalletConnect (Python)

A Python client for [WalletConnect v2](https://specs.walletconnect.com/2.0) (now [Reown](https://reown.com)),
so Python tools (e.g. [Ape](https://github.com/ApeWorX/ape) account plugins) can pair with mobile wallets
and relay signing requests to them.

## Status

> [!WARNING]
> This project is being revived and is **pre-alpha**. There is no working protocol implementation yet.

- **Target:** WalletConnect v2 / Reown **Sign API** (relay at `relay.walletconnect.com`, `irn` JSON-RPC,
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

TODO: Usage examples will be added once the v2 Sign API client lands.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Comments, questions, criticisms and pull requests are welcomed.

## License

This project is licensed under the [Apache 2.0](LICENSE).
