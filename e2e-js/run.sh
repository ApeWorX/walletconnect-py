#!/usr/bin/env bash
# Orchestrate Python dApp ↔ JS @walletconnect/sign-client wallet E2E.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
ENV_FILE="$(cd "$ROOT/../.." && pwd)/.env.local"
if [[ ! -f "$ENV_FILE" ]]; then
  ENV_FILE="$REPO/.env.local"
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${WALLETCONNECT_PROJECT_ID:-}" ]]; then
  echo "WALLETCONNECT_PROJECT_ID not set (source .env.local or export it)" >&2
  exit 1
fi

URI_FILE="${TMPDIR:-/tmp}/wc-e2e-uri-$$.txt"
DONE_FILE="${TMPDIR:-/tmp}/wc-e2e-done-$$.txt"
rm -f "$URI_FILE" "$DONE_FILE"

TYPED_ARGS=()
WALLET_TYPED=()
if [[ "${1:-}" == "--typed-data" ]]; then
  TYPED_ARGS=(--typed-data)
  WALLET_TYPED=(--typed-data)
fi

cleanup() {
  if [[ -n "${WALLET_PID:-}" ]] && kill -0 "$WALLET_PID" 2>/dev/null; then
    kill "$WALLET_PID" 2>/dev/null || true
    wait "$WALLET_PID" 2>/dev/null || true
  fi
  rm -f "$URI_FILE" "$DONE_FILE"
}
trap cleanup EXIT

cd "$ROOT"
echo "[run] starting JS wallet…"
node wallet.mjs --uri-file "$URI_FILE" --done-file "$DONE_FILE" "${WALLET_TYPED[@]}" &
WALLET_PID=$!

echo "[run] starting Python dApp…"
cd "$REPO"
set +e
uv run python "$ROOT/dapp.py" --uri-file "$URI_FILE" "${TYPED_ARGS[@]}"
DAPP_RC=$?
set -e

# Wait briefly for wallet to flush DONE
for _ in $(seq 1 20); do
  if [[ -f "$DONE_FILE" ]]; then
    break
  fi
  sleep 0.25
done

if [[ "$DAPP_RC" -ne 0 ]]; then
  echo "[run] dApp failed rc=$DAPP_RC" >&2
  exit "$DAPP_RC"
fi

if [[ ! -f "$DONE_FILE" ]]; then
  echo "[run] wallet did not write DONE file" >&2
  exit 1
fi

echo "[run] wallet done: $(cat "$DONE_FILE")"
echo "[run] E2E PASSED"
exit 0
