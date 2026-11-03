"""JSON-RPC id helpers compatible with ``@walletconnect/safe-json`` / JS Sign client."""

from __future__ import annotations

from typing import Any

# Number.MAX_SAFE_INTEGER — beyond this, JS JSON.parse loses precision.
JS_MAX_SAFE_INTEGER = 2**53 - 1


def coerce_rpc_id(value: Any) -> int:
    """
    Normalize a JSON-RPC ``id`` to ``int``.

    Accepts ints, numeric strings, and the ``"<digits>n"`` form produced by
    ``@walletconnect/safe-json`` when stringifying BigInt ids.
    """
    if isinstance(value, bool):  # bool is a subclass of int — reject
        raise ValueError(f"Invalid JSON-RPC id: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"Invalid JSON-RPC id: {value!r}")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("n", "N")) and text[:-1].lstrip("-").isdigit():
            text = text[:-1]
        return int(text, 10)
    raise ValueError(f"Invalid JSON-RPC id: {value!r}")


def encode_rpc_id(value: Any) -> int:
    """
    Encode an id for JSON serialization as a JS-safe number.

    The official JS Sign client validates ids with ``isValidId`` which requires a
    ``number`` (BigInt and ``"<digits>n"`` strings are rejected and the proposal
    is auto-rejected with tag 1121).
    """
    as_int = coerce_rpc_id(value)
    if abs(as_int) > JS_MAX_SAFE_INTEGER:
        raise ValueError(
            f"JSON-RPC id {as_int} exceeds JS MAX_SAFE_INTEGER; "
            "use payload_id() with default entropy so peers accept the id"
        )
    return as_int
