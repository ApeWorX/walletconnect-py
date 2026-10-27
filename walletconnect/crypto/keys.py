"""
X25519 key agreement and HKDF-SHA256 symmetric-key derivation.

Matches ``@walletconnect/utils`` crypto helpers:
https://github.com/WalletConnect/walletconnect-monorepo/blob/v2.0/packages/utils/src/crypto.ts
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from walletconnect.exceptions import InvalidKeyError

_KEY_LEN = 32
_HEX_KEY_LEN = 64


def parse_hex_key(value: str, *, name: str) -> bytes:
    if len(value) != _HEX_KEY_LEN or any(c not in "0123456789abcdef" for c in value):
        raise InvalidKeyError(f"{name} must be {_HEX_KEY_LEN} lowercase hex characters")
    return bytes.fromhex(value)


@dataclass(frozen=True, slots=True)
class KeyPair:
    """An X25519 key pair, both halves stored as lowercase hex."""

    private_key: str
    public_key: str

    @property
    def private_key_bytes(self) -> bytes:
        return bytes.fromhex(self.private_key)

    @property
    def public_key_bytes(self) -> bytes:
        return bytes.fromhex(self.public_key)


def generate_key_pair() -> KeyPair:
    """Generate a fresh X25519 key pair."""
    private = X25519PrivateKey.from_private_bytes(os.urandom(_KEY_LEN))
    return KeyPair(
        private_key=private.private_bytes_raw().hex(),
        public_key=private.public_key().public_bytes_raw().hex(),
    )


def generate_random_bytes32() -> str:
    """Return 32 cryptographically random bytes as lowercase hex (a pairing symKey)."""
    return secrets.token_hex(_KEY_LEN)


def derive_sym_key(private_key_a: str, public_key_b: str) -> str:
    """
    Derive a 32-byte symmetric key via X25519 + HKDF-SHA256.

    Salt defaults to 32 zero bytes and info is empty, matching the JS SDK
    (``hkdf(sha256, shared, undefined, undefined, 32)``).
    """
    private = X25519PrivateKey.from_private_bytes(parse_hex_key(private_key_a, name="private_key"))
    public = X25519PublicKey.from_public_bytes(parse_hex_key(public_key_b, name="public_key"))
    shared = private.exchange(public)
    sym = HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=b"").derive(shared)
    return sym.hex()


def hash_key(key: str) -> str:
    """SHA-256 of a hex-decoded key, returned as lowercase hex (used as a topic)."""
    return hashlib.sha256(parse_hex_key(key, name="key")).hexdigest()


def topic_from_sym_key(sym_key: str) -> str:
    """Return the WalletConnect topic for a symmetric key (``sha256(symKey).hex()``)."""
    return hash_key(sym_key)
