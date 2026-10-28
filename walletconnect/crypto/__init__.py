"""X25519 / HKDF / ChaCha20-Poly1305 primitives and Ed25519 relay auth."""

from .did import decode_did_key, encode_did_key
from .envelope import (
    BASE64,
    BASE64URL,
    TYPE_0,
    TYPE_1,
    decrypt,
    deserialize,
    encrypt,
    serialize,
)
from .jwt import (
    CLIENT_AUTH_ACT,
    DEFAULT_JWT_TTL,
    AuthKeyPair,
    decode_jwt,
    generate_auth_key_pair,
    sign_jwt,
    verify_jwt,
)
from .keys import (
    KeyPair,
    derive_sym_key,
    generate_key_pair,
    generate_random_bytes32,
    hash_key,
    topic_from_sym_key,
)

__all__ = [
    "BASE64",
    "BASE64URL",
    "CLIENT_AUTH_ACT",
    "DEFAULT_JWT_TTL",
    "TYPE_0",
    "TYPE_1",
    "AuthKeyPair",
    "KeyPair",
    "decode_did_key",
    "decode_jwt",
    "decrypt",
    "derive_sym_key",
    "deserialize",
    "encode_did_key",
    "encrypt",
    "generate_auth_key_pair",
    "generate_key_pair",
    "generate_random_bytes32",
    "hash_key",
    "serialize",
    "sign_jwt",
    "topic_from_sym_key",
    "verify_jwt",
]
