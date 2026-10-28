"""
EdDSA (Ed25519) did-JWTs for WalletConnect relay authentication.

Spec:
https://specs.walletconnect.com/2.0/specs/clients/core/relay/relay-client-auth
https://specs.walletconnect.com/2.0/specs/clients/core/crypto/crypto-authentication
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from walletconnect.exceptions import InvalidJWTError, InvalidKeyError
from walletconnect.utils.encoding import b64url_decode, b64url_encode

from .did import decode_did_key, encode_did_key

CLIENT_AUTH_ACT = "client_auth"
# Match @walletconnect/core CRYPTO_JWT_TTL (one day).
DEFAULT_JWT_TTL = 86400

_JWT_HEADER = {"alg": "EdDSA", "typ": "JWT"}


@dataclass(frozen=True, slots=True)
class AuthKeyPair:
    """A persistent Ed25519 key pair used as the relay ``client_id``."""

    private_key: str  # 64 lowercase hex chars (32-byte seed)
    public_key: str  # 64 lowercase hex chars

    @property
    def did_key(self) -> str:
        return encode_did_key(self.public_key)

    @property
    def private_key_bytes(self) -> bytes:
        return bytes.fromhex(self.private_key)

    @property
    def public_key_bytes(self) -> bytes:
        return bytes.fromhex(self.public_key)


def generate_auth_key_pair(seed: bytes | str | None = None) -> AuthKeyPair:
    """
    Generate (or restore) an Ed25519 auth key pair.

    When ``seed`` is given (32 bytes or 64 hex chars) the key pair is
    deterministic, matching ``ed25519.generateKeyPairFromSeed`` in the JS SDK.
    """
    if seed is None:
        seed_bytes = os.urandom(32)
    elif isinstance(seed, str):
        if len(seed) != 64 or any(c not in "0123456789abcdef" for c in seed):
            raise InvalidKeyError("seed must be 64 lowercase hex characters")
        seed_bytes = bytes.fromhex(seed)
    else:
        seed_bytes = seed
    if len(seed_bytes) != 32:
        raise InvalidKeyError("Ed25519 seed must be 32 bytes")
    private = Ed25519PrivateKey.from_private_bytes(seed_bytes)
    return AuthKeyPair(
        private_key=seed_bytes.hex(),
        public_key=private.public_key().public_bytes_raw().hex(),
    )


def _encode_json(value: Any) -> str:
    # Compact separators and insertion order matter: the signature covers the
    # exact UTF-8 bytes of header.payload.
    return b64url_encode(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def _decode_json(value: str) -> Any:
    return json.loads(b64url_decode(value).decode("utf-8"))


def sign_jwt(
    *,
    aud: str,
    key_pair: AuthKeyPair,
    sub: str | None = None,
    ttl: int = DEFAULT_JWT_TTL,
    iat: int | None = None,
    act: str = CLIENT_AUTH_ACT,
) -> str:
    """
    Build a relay auth did-JWT.

    Payload claim order matches the WalletConnect reference implementation:
    ``iat``, ``exp``, ``act``, ``iss``, ``sub``, ``aud``.
    """
    if ttl <= 0:
        raise ValueError("ttl must be positive")
    issued_at = int(time.time()) if iat is None else iat
    subject = sub if sub is not None else os.urandom(32).hex()
    header = dict(_JWT_HEADER)
    payload = {
        "iat": issued_at,
        "exp": issued_at + ttl,
        "act": act,
        "iss": key_pair.did_key,
        "sub": subject,
        "aud": aud,
    }
    signing_input = f"{_encode_json(header)}.{_encode_json(payload)}".encode()
    private = Ed25519PrivateKey.from_private_bytes(key_pair.private_key_bytes)
    signature = private.sign(signing_input)
    return f"{_encode_json(header)}.{_encode_json(payload)}.{b64url_encode(signature)}"


def decode_jwt(jwt: str) -> dict[str, Any]:
    """Split a JWT into ``header``, ``payload``, and raw ``signature`` bytes."""
    try:
        header_b64, payload_b64, signature_b64 = jwt.split(".")
    except ValueError as exc:
        raise InvalidJWTError("JWT must have three base64url parts") from exc
    try:
        header = _decode_json(header_b64)
        payload = _decode_json(payload_b64)
        signature = b64url_decode(signature_b64)
    except Exception as exc:  # noqa: BLE001
        raise InvalidJWTError("JWT parts are not valid base64url JSON") from exc
    return {"header": header, "payload": payload, "signature": signature}


def verify_jwt(jwt: str, *, now: int | None = None) -> dict[str, Any]:
    """
    Verify an EdDSA did-JWT and return its payload.

    Checks the header, recovers the public key from ``iss``, verifies the
    signature, and rejects expired tokens (when ``exp`` is present).
    """
    parts = decode_jwt(jwt)
    header = parts["header"]
    payload = parts["payload"]
    signature = parts["signature"]
    if header.get("alg") != "EdDSA" or header.get("typ") != "JWT":
        raise InvalidJWTError("JWT must use EdDSA algorithm")
    if "iss" not in payload:
        raise InvalidJWTError("JWT payload missing iss")
    try:
        public_key = decode_did_key(payload["iss"])
    except InvalidKeyError as exc:
        raise InvalidJWTError(str(exc)) from exc

    header_b64, payload_b64, _ = jwt.split(".")
    signing_input = f"{header_b64}.{payload_b64}".encode()
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signing_input)
    except Exception as exc:  # noqa: BLE001
        raise InvalidJWTError("JWT signature is invalid") from exc

    current = int(time.time()) if now is None else now
    exp = payload.get("exp")
    if isinstance(exp, int) and current >= exp:
        raise InvalidJWTError("JWT has expired")
    return payload


# Re-export encode/decode did helpers for callers that import from jwt.
__all__ = [
    "CLIENT_AUTH_ACT",
    "DEFAULT_JWT_TTL",
    "AuthKeyPair",
    "InvalidJWTError",
    "decode_jwt",
    "generate_auth_key_pair",
    "sign_jwt",
    "verify_jwt",
]
