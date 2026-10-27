import base64

import pytest
from hypothesis import (
    given,
    strategies as st,
)

from walletconnect.crypto.envelope import (
    BASE64URL,
    TYPE_0,
    TYPE_1,
    decrypt,
    deserialize,
    encrypt,
    serialize,
)
from walletconnect.crypto.keys import derive_sym_key, generate_key_pair
from walletconnect.exceptions import InvalidEnvelopeError

# Cross-checked against @noble/ciphers ChaCha20-Poly1305 (see PR description).
PRIV_A = "01" * 32
PUB_A = "a4e09292b651c278b9772c569f5fa9bb13d906b46ab68c9df9dc2b4409f8a209"
PRIV_B = "02" * 32
SYM = "cfe64b3a58371451aa931b549552ee76ba01b9e87e196eb7fe91ad4594913244"
IV = bytes(12)
MESSAGE = '{"id":1}'
TYPE0 = "AAAAAAAAAAAAAAAAAMWaevQ3Wky2eTrIaKSkux021sBlNH6RTg=="
TYPE1 = (
    "AaTgkpK2UcJ4uXcsVp9fqbsT2Qa0araMnfncK0QJ+KIJAAAAAAAAAAAAAAAAxZp69DdaTLZ5OshopKS7HTbWwGU0fpFO"
)


def test_type0_vector():
    assert encrypt(sym_key=SYM, message=MESSAGE, iv=IV, type=TYPE_0) == TYPE0
    assert decrypt(sym_key=SYM, encoded=TYPE0) == MESSAGE


def test_type1_vector():
    encoded = encrypt(
        sym_key=SYM,
        message=MESSAGE,
        iv=IV,
        type=TYPE_1,
        sender_public_key=PUB_A,
    )
    assert encoded == TYPE1
    assert decrypt(sym_key=SYM, encoded=TYPE1) == MESSAGE
    parts = deserialize(TYPE1)
    assert parts["type"] == TYPE_1
    assert parts["sender_public_key"] == bytes.fromhex(PUB_A)


def test_type0_round_trip_random_iv():
    encoded = encrypt(sym_key=SYM, message=MESSAGE)
    assert decrypt(sym_key=SYM, encoded=encoded) == MESSAGE
    assert deserialize(encoded)["type"] == TYPE_0


def test_type1_with_derived_key():
    a = generate_key_pair()
    b = generate_key_pair()
    sym = derive_sym_key(a.private_key, b.public_key)
    assert sym == derive_sym_key(b.private_key, a.public_key)
    encoded = encrypt(
        sym_key=sym,
        message=MESSAGE,
        type=TYPE_1,
        sender_public_key=a.public_key,
    )
    assert decrypt(sym_key=sym, encoded=encoded) == MESSAGE


def test_base64url_round_trip():
    encoded = encrypt(sym_key=SYM, message=MESSAGE, iv=IV, encoding=BASE64URL)
    assert "+" not in encoded
    assert "/" not in encoded
    assert "=" not in encoded
    assert decrypt(sym_key=SYM, encoded=encoded, encoding=BASE64URL) == MESSAGE


def test_wrong_key_fails():
    encoded = encrypt(sym_key=SYM, message=MESSAGE, iv=IV)
    with pytest.raises(InvalidEnvelopeError, match="decrypt"):
        decrypt(sym_key="00" * 32, encoded=encoded)


def test_type1_requires_sender():
    with pytest.raises(InvalidEnvelopeError, match="sender"):
        encrypt(sym_key=SYM, message=MESSAGE, type=TYPE_1)


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        base64.b64encode(b"").decode(),
        base64.b64encode(bytes([2, 0])).decode(),  # unsupported type
        base64.b64encode(bytes([0, 1, 2])).decode(),  # truncated type 0
        base64.b64encode(bytes([1]) + b"\x00" * 10).decode(),  # truncated type 1
    ],
)
def test_deserialize_rejects(encoded):
    with pytest.raises(InvalidEnvelopeError):
        deserialize(encoded)


def test_serialize_rejects_bad_iv():
    with pytest.raises(InvalidEnvelopeError, match="IV"):
        serialize(type=TYPE_0, iv=b"short", sealed=b"\x00" * 16)


@given(st.text(min_size=0, max_size=200))
def test_encrypt_decrypt_property(message):
    encoded = encrypt(sym_key=SYM, message=message)
    assert decrypt(sym_key=SYM, encoded=encoded) == message
