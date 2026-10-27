import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from hypothesis import (
    given,
    strategies as st,
)

from walletconnect.crypto.keys import (
    KeyPair,
    derive_sym_key,
    generate_key_pair,
    generate_random_bytes32,
    hash_key,
    parse_hex_key,
    topic_from_sym_key,
)
from walletconnect.exceptions import InvalidKeyError

# Cross-checked against @noble/curves + @noble/hashes (see PR description).
PRIV_A = "01" * 32
PRIV_B = "02" * 32
PUB_A = "a4e09292b651c278b9772c569f5fa9bb13d906b46ab68c9df9dc2b4409f8a209"
SYM = "cfe64b3a58371451aa931b549552ee76ba01b9e87e196eb7fe91ad4594913244"
TOPIC = "7ea0d8e3085cce12bf0ce3d9f6e832632dce2a10232d8b950c3ff9fee3fd9732"
PUB_B = (
    X25519PrivateKey.from_private_bytes(bytes.fromhex(PRIV_B)).public_key().public_bytes_raw().hex()
)


def test_generate_key_pair_shape():
    pair = generate_key_pair()
    assert isinstance(pair, KeyPair)
    assert len(pair.private_key) == 64
    assert len(pair.public_key) == 64
    assert pair.private_key_bytes == bytes.fromhex(pair.private_key)
    assert generate_key_pair() != pair


def test_generate_random_bytes32():
    value = generate_random_bytes32()
    assert len(value) == 64
    assert value != generate_random_bytes32()


def test_derive_sym_key_matches_js_vector():
    assert derive_sym_key(PRIV_A, PUB_B) == SYM
    # Shared secret is commutative → same sym key from B's side.
    assert derive_sym_key(PRIV_B, PUB_A) == SYM


def test_topic_from_sym_key():
    assert topic_from_sym_key(SYM) == TOPIC
    assert hash_key(SYM) == TOPIC
    assert topic_from_sym_key(SYM) == hashlib.sha256(bytes.fromhex(SYM)).hexdigest()


@pytest.mark.parametrize(
    "bad",
    ["", "ab", "01" * 31, "01" * 33, "GG" * 32, "AB" * 32],
)
def test_parse_hex_key_rejects(bad):
    with pytest.raises(InvalidKeyError):
        parse_hex_key(bad, name="key")


@given(st.binary(min_size=32, max_size=32), st.binary(min_size=32, max_size=32))
def test_derive_is_deterministic(priv, peer_seed):
    peer_pub = X25519PrivateKey.from_private_bytes(peer_seed).public_key().public_bytes_raw().hex()
    a = derive_sym_key(priv.hex(), peer_pub)
    assert a == derive_sym_key(priv.hex(), peer_pub)
    assert len(a) == 64
