# Testing

The unit test suite runs offline:

```bash
uv sync --group test
uv run pytest
```

Tests marked `live` talk to the real WalletConnect relay. They are skipped unless
`WALLETCONNECT_PROJECT_ID` is set. Never commit a project ID.

```bash
export WALLETCONNECT_PROJECT_ID=your_project_id
uv run pytest -m live
```

The `e2e-js/` directory contains an interoperability harness that runs the official JavaScript
Sign client as the wallet peer against this Python dApp client. See its README for setup.
