# Python dApp ↔ JS Sign Client E2E

Interoperability harness: local `walletconnect-py` (dApp / proposer) against
official `@walletconnect/sign-client` (wallet / responder) over the public
WalletConnect relay.

This directory is **not** part of the publishable Python package.

## Setup

```bash
# Project ID (do not commit) — e.g. from a sibling .env.local in your workspace
set -a && source ../../.env.local && set +a   # adjust path as needed
# or: export WALLETCONNECT_PROJECT_ID=…

cd e2e-js
npm install
cd .. && uv sync --group test
```

## Run

```bash
cd e2e-js
./run.sh                 # personal_sign only
./run.sh --typed-data    # + eth_signTypedData_v4
```

Exit `0` and `[run] E2E PASSED` mean pair → settle → sign succeeded.

## What this proves vs mobile

Same protocol path a phone wallet uses (relay + Sign session + `personal_sign`).
Gaps vs a manual MetaMask scan: no deep-link/UI, fixed test key
(`Anvil #0`), and no Verify attestation UX.
