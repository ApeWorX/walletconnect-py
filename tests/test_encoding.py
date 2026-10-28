import pytest

from walletconnect.utils.encoding import b58decode, b58encode, b64url_decode, b64url_encode


def test_b64url_round_trip():
    raw = bytes(range(256))
    encoded = b64url_encode(raw)
    assert "+" not in encoded
    assert "/" not in encoded
    assert "=" not in encoded
    assert b64url_decode(encoded) == raw


def test_b58_round_trip_and_leading_zeros():
    assert b58encode(b"\x00\x00\x01") == "112"
    assert b58decode("112") == b"\x00\x00\x01"
    raw = bytes.fromhex("ed01884ab67f787b69e534bfdba8d5beb4e719700e90ac06317ed177d49e5a33be5a")
    assert b58decode(b58encode(raw)) == raw


def test_b58_rejects_invalid():
    with pytest.raises(ValueError, match="base58"):
        b58decode("0OIl")  # characters not in the alphabet
