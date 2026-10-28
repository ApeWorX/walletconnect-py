"""Encode/decode JSON-RPC payloads as WalletConnect typed envelopes."""

from __future__ import annotations

import json
from typing import Any

from walletconnect.utils.rpc_id import encode_rpc_id

from .envelope import TYPE_0, decrypt, encrypt


def _prepare_for_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with a JS-safe ``id`` field when present."""
    if "id" not in payload:
        return payload
    out = dict(payload)
    out["id"] = encode_rpc_id(payload["id"])
    return out


def encode_jsonrpc(
    payload: dict[str, Any],
    *,
    sym_key: str,
    type: int = TYPE_0,
    sender_public_key: str | None = None,
) -> str:
    """Serialize ``payload`` to JSON and seal it in a typed envelope."""
    message = json.dumps(_prepare_for_json(payload), separators=(",", ":"), ensure_ascii=False)
    return encrypt(
        sym_key=sym_key,
        message=message,
        type=type,
        sender_public_key=sender_public_key,
    )


def decode_jsonrpc(encoded: str, *, sym_key: str) -> dict[str, Any]:
    """Open a typed envelope and parse the JSON-RPC payload."""
    plaintext = decrypt(sym_key=sym_key, encoded=encoded)
    payload = json.loads(plaintext)
    if not isinstance(payload, dict):
        raise ValueError("Envelope plaintext must be a JSON object")
    return payload
