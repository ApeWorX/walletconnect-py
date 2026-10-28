"""
WalletConnect typed envelopes (ChaCha20-Poly1305).

Spec: https://specs.walletconnect.com/2.0/specs/clients/core/crypto/crypto-envelopes

- Type 0: ``0x00 ‖ iv(12) ‖ sealbox`` when both peers already share ``symKey``.
- Type 1: ``0x01 ‖ sender_pk(32) ‖ iv(12) ‖ sealbox`` when the receiver must derive
  the key from the embedded sender public key.

The default wire encoding is standard base64 with padding (``base64pad`` in the
JS SDK). Pass ``encoding="base64url"`` for the unpadded URL-safe variant.
"""

from __future__ import annotations

import base64
import os
from typing import Literal

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from walletconnect.exceptions import InvalidEnvelopeError

from .keys import parse_hex_key

TYPE_0 = 0
TYPE_1 = 1

BASE64 = "base64"
BASE64URL = "base64url"

Encoding = Literal["base64", "base64url"]

_TYPE_LEN = 1
_IV_LEN = 12
_KEY_LEN = 32
_TAG_LEN = 16
_MIN_TYPE0 = _TYPE_LEN + _IV_LEN + _TAG_LEN
_MIN_TYPE1 = _TYPE_LEN + _KEY_LEN + _IV_LEN + _TAG_LEN


def _encode(raw: bytes, encoding: Encoding) -> str:
    if encoding == BASE64URL:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if encoding == BASE64:
        return base64.b64encode(raw).decode("ascii")
    raise ValueError(f"Unsupported encoding {encoding!r}")


def _decode(encoded: str, encoding: Encoding) -> bytes:
    try:
        if encoding == BASE64URL:
            padded = encoded + "=" * (-len(encoded) % 4)
            return base64.urlsafe_b64decode(padded)
        if encoding == BASE64:
            return base64.b64decode(encoded, validate=False)
    except Exception as exc:  # noqa: BLE001 - surface as InvalidEnvelopeError
        raise InvalidEnvelopeError(f"Invalid {encoding} envelope") from exc
    raise ValueError(f"Unsupported encoding {encoding!r}")


def serialize(
    *,
    type: int,
    iv: bytes,
    sealed: bytes,
    sender_public_key: bytes | None = None,
    encoding: Encoding = "base64",
) -> str:
    """Serialize a typed envelope to a base64 string."""
    if type == TYPE_1:
        if sender_public_key is None or len(sender_public_key) != _KEY_LEN:
            raise InvalidEnvelopeError("Type 1 envelope requires a 32-byte sender public key")
        raw = bytes([TYPE_1]) + sender_public_key + iv + sealed
    elif type == TYPE_0:
        raw = bytes([TYPE_0]) + iv + sealed
    else:
        raise InvalidEnvelopeError(f"Unsupported envelope type {type}")
    if len(iv) != _IV_LEN:
        raise InvalidEnvelopeError(f"IV must be {_IV_LEN} bytes")
    return _encode(raw, encoding)


def deserialize(
    encoded: str,
    *,
    encoding: Encoding = "base64",
) -> dict[str, bytes | int]:
    """
    Split a base64 envelope into ``type``, ``iv``, ``sealed``, and optionally
    ``sender_public_key``.
    """
    raw = _decode(encoded, encoding)
    if not raw:
        raise InvalidEnvelopeError("Empty envelope")
    envelope_type = raw[0]
    if envelope_type == TYPE_1:
        if len(raw) < _MIN_TYPE1:
            raise InvalidEnvelopeError("Type 1 envelope is truncated")
        sender = raw[_TYPE_LEN : _TYPE_LEN + _KEY_LEN]
        iv = raw[_TYPE_LEN + _KEY_LEN : _TYPE_LEN + _KEY_LEN + _IV_LEN]
        sealed = raw[_TYPE_LEN + _KEY_LEN + _IV_LEN :]
        return {"type": TYPE_1, "sender_public_key": sender, "iv": iv, "sealed": sealed}
    if envelope_type == TYPE_0:
        if len(raw) < _MIN_TYPE0:
            raise InvalidEnvelopeError("Type 0 envelope is truncated")
        iv = raw[_TYPE_LEN : _TYPE_LEN + _IV_LEN]
        sealed = raw[_TYPE_LEN + _IV_LEN :]
        return {"type": TYPE_0, "iv": iv, "sealed": sealed}
    raise InvalidEnvelopeError(f"Unsupported envelope type {envelope_type}")


def encrypt(
    *,
    sym_key: str,
    message: str | bytes,
    type: int = TYPE_0,
    sender_public_key: str | None = None,
    iv: bytes | str | None = None,
    encoding: Encoding = "base64",
) -> str:
    """
    Seal ``message`` under ``sym_key`` and return a typed envelope string.

    ``message`` is UTF-8 encoded when given as ``str``. ``iv`` may be 12 raw
    bytes or 24 hex characters; a fresh IV is generated when omitted.
    """
    key = parse_hex_key(sym_key, name="sym_key")
    if isinstance(iv, str):
        iv_bytes = bytes.fromhex(iv)
    elif iv is None:
        iv_bytes = os.urandom(_IV_LEN)
    else:
        iv_bytes = iv
    if len(iv_bytes) != _IV_LEN:
        raise InvalidEnvelopeError(f"IV must be {_IV_LEN} bytes")

    plaintext = message.encode("utf-8") if isinstance(message, str) else message
    sealed = ChaCha20Poly1305(key).encrypt(iv_bytes, plaintext, None)

    sender_bytes: bytes | None = None
    if type == TYPE_1:
        if sender_public_key is None:
            raise InvalidEnvelopeError("Missing sender public key for type 1 envelope")
        sender_bytes = parse_hex_key(sender_public_key, name="sender_public_key")

    return serialize(
        type=type,
        iv=iv_bytes,
        sealed=sealed,
        sender_public_key=sender_bytes,
        encoding=encoding,
    )


def decrypt(
    *,
    sym_key: str,
    encoded: str,
    encoding: Encoding = "base64",
) -> str:
    """Open a typed envelope and return the UTF-8 plaintext."""
    key = parse_hex_key(sym_key, name="sym_key")
    parts = deserialize(encoded, encoding=encoding)
    iv = parts["iv"]
    sealed = parts["sealed"]
    if not isinstance(iv, bytes) or not isinstance(sealed, bytes):
        raise TypeError("internal envelope decode error")
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(iv, sealed, None)
    except Exception as exc:  # noqa: BLE001 - cryptography raises InvalidTag
        raise InvalidEnvelopeError("Failed to decrypt envelope") from exc
    return plaintext.decode("utf-8")
