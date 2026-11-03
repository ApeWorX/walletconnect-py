"""
``did:key`` encoding for Ed25519 public keys (multibase base58btc + multicodec).

Spec: https://specs.walletconnect.com/2.0/specs/clients/core/crypto/crypto-authentication
"""

from __future__ import annotations

from walletconnect.exceptions import InvalidKeyError
from walletconnect.utils.encoding import b58decode, b58encode

_DID_PREFIX = "did:key:z"
# Multicodec varint for ed25519-pub (0xed01), which base58btc-encodes as "K36".
_ED25519_MULTICODEC = bytes.fromhex("ed01")
_ED25519_MULTICODEC_B58 = "K36"
_PUB_LEN = 32


def encode_did_key(public_key: bytes | str) -> str:
    """
    Encode an Ed25519 public key as ``did:key:z…``.

    ``public_key`` may be 32 raw bytes or 64 lowercase hex characters.
    """
    if isinstance(public_key, str):
        if len(public_key) != 64 or any(c not in "0123456789abcdef" for c in public_key):
            raise InvalidKeyError("public_key must be 64 lowercase hex characters")
        public_key = bytes.fromhex(public_key)
    if len(public_key) != _PUB_LEN:
        raise InvalidKeyError(f"Ed25519 public key must be {_PUB_LEN} bytes")
    return _DID_PREFIX + b58encode(_ED25519_MULTICODEC + public_key)


def decode_did_key(did: str) -> bytes:
    """Decode a ``did:key:z…`` issuer string to the raw 32-byte Ed25519 public key."""
    if not did.startswith(_DID_PREFIX):
        raise InvalidKeyError('Issuer must be a DID with method "key" and multibase "z"')
    try:
        multicodec = b58decode(did[len(_DID_PREFIX) :])
    except ValueError as exc:
        raise InvalidKeyError("Issuer multibase is not valid base58btc") from exc
    header, public_key = multicodec[:2], multicodec[2:]
    if header != _ED25519_MULTICODEC and b58encode(header) != _ED25519_MULTICODEC_B58:
        raise InvalidKeyError('Issuer must be a public key with type "Ed25519"')
    if len(public_key) != _PUB_LEN:
        raise InvalidKeyError(f"Issuer public key must be {_PUB_LEN} bytes")
    return public_key
