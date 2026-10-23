import hashlib
import time

import pytest
from hypothesis import (
    given,
    strategies as st,
)

from walletconnect.exceptions import InvalidPairingURIError
from walletconnect.pairing.uri import PairingURI, format_methods, parse_methods

# Example from the spec: https://specs.walletconnect.com/2.0/specs/clients/core/pairing/pairing-uri
TOPIC = "7f6e504bfad60b485450578e05678ed3e8e8c4751d3c6160be17160d63ec90f9"
SYM_KEY = "587d5484ce2a2a6ee3ba1962fdd7e8588e06200c46823bd18fbd67def96ad303"
SPEC_URI = (
    f"wc:{TOPIC}@2?symKey={SYM_KEY}"
    "&methods=[wc_sessionPropose],[wc_authRequest,wc_authBatchRequest]"
    "&relay-protocol=irn&expiryTimestamp=1705667684"
)


def test_parse_spec_example():
    uri = PairingURI.parse(SPEC_URI)
    assert uri.topic == TOPIC
    assert uri.sym_key == SYM_KEY
    assert uri.version == 2
    assert uri.relay_protocol == "irn"
    assert uri.relay_data is None
    assert uri.expiry_timestamp == 1705667684
    assert uri.methods == (("wc_sessionPropose",), ("wc_authRequest", "wc_authBatchRequest"))
    assert uri.extra == {}
    assert uri.is_expired()


def test_format_round_trip():
    uri = PairingURI.parse(SPEC_URI)
    formatted = uri.format()
    assert formatted.startswith(f"wc:{TOPIC}@2?relay-protocol=irn&symKey={SYM_KEY}")
    assert "methods=[wc_sessionPropose],[wc_authRequest,wc_authBatchRequest]" in formatted
    assert PairingURI.parse(formatted) == uri
    assert str(uri) == formatted


def test_parse_percent_encoded():
    # e.g. as emitted by `query-string` in the JS SDK
    encoded = SPEC_URI.replace("[", "%5B").replace("]", "%5D").replace(",", "%2C")
    assert PairingURI.parse(encoded) == PairingURI.parse(SPEC_URI)


def test_parse_minimal_sdk_style():
    # Current JS SDKs often omit `methods`.
    uri = PairingURI.parse(f"wc:{TOPIC}@2?relay-protocol=irn&symKey={SYM_KEY}")
    assert uri.methods is None
    assert uri.expiry_timestamp is None
    assert not uri.is_expired()
    assert "methods" not in uri.format()
    assert "expiryTimestamp" not in uri.format()


def test_unknown_params_preserved():
    uri = PairingURI.parse(
        f"wc:{TOPIC}@2?relay-protocol=irn&symKey={SYM_KEY}&relay-data=abcd&foo=bar%20baz"
    )
    assert uri.relay_data == "abcd"
    assert uri.extra == {"foo": "bar baz"}
    again = PairingURI.parse(uri.format())
    assert again.extra == {"foo": "bar baz"}
    assert again.relay_data == "abcd"


def test_generate():
    before = int(time.time())
    uri = PairingURI.generate()
    assert uri.topic == hashlib.sha256(uri.sym_key_bytes).hexdigest()
    assert len(uri.sym_key_bytes) == 32
    assert uri.expiry_timestamp is not None
    assert before + 300 <= uri.expiry_timestamp <= int(time.time()) + 300
    assert not uri.is_expired()
    assert uri.is_expired(now=uri.expiry_timestamp)
    assert PairingURI.parse(uri.format()) == uri
    assert PairingURI.generate() != uri


def test_generate_options():
    uri = PairingURI.generate(expiry=None, methods=(("wc_sessionPropose",),))
    assert uri.expiry_timestamp is None
    assert "methods=[wc_sessionPropose]" in uri.format()


@pytest.mark.parametrize(
    ("uri", "match"),
    [
        ("https://example.com", "Not a WalletConnect URI"),
        (f"wc:{TOPIC}?relay-protocol=irn&symKey={SYM_KEY}", "Missing WalletConnect version"),
        (f"wc:{TOPIC}@1?bridge=https%3A%2F%2Fbridge.walletconnect.org&key=00", "v1"),
        (f"wc:{TOPIC}@3?relay-protocol=irn&symKey={SYM_KEY}", "Unsupported WalletConnect version"),
        (f"wc:{TOPIC}@2?relay-protocol=irn", "Missing 'symKey'"),
        (f"wc:{TOPIC}@2?symKey={SYM_KEY}", "Missing 'relay-protocol'"),
        (f"wc:{TOPIC}@2?relay-protocol=&symKey={SYM_KEY}", "Missing 'relay-protocol'"),
        (f"wc:abc@2?relay-protocol=irn&symKey={SYM_KEY}", "'topic'"),
        (f"wc:{TOPIC}@2?relay-protocol=irn&symKey={SYM_KEY.upper()}", "'symKey'"),
        (f"wc:{TOPIC}@2?relay-protocol=irn&symKey={SYM_KEY}&symKey={SYM_KEY}", "Duplicate"),
        (f"wc:{TOPIC}@2?relay-protocol=irn&symKey={SYM_KEY}&expiryTimestamp=soon", "expiry"),
        (f"wc:{TOPIC}@2?relay-protocol=irn&symKey={SYM_KEY}&methods=wc_x", "methods"),
    ],
)
def test_parse_invalid(uri, match):
    with pytest.raises(InvalidPairingURIError, match=match):
        PairingURI.parse(uri)


def test_invalid_uri_is_value_error():
    with pytest.raises(ValueError, match="Not a WalletConnect URI"):
        PairingURI.parse("nope")


def test_parse_methods():
    assert parse_methods("") == ()
    assert parse_methods("[a],[b, c]") == (("a",), ("b", "c"))
    assert parse_methods("[]") == ((),)
    assert format_methods((("a",), ("b", "c"))) == "[a],[b,c]"
    with pytest.raises(InvalidPairingURIError):
        parse_methods("[a]x[b]")


_method = st.from_regex(r"\A[a-zA-Z_]{1,20}\Z")


@given(
    sym_key=st.binary(min_size=32, max_size=32),
    expiry=st.none() | st.integers(min_value=0, max_value=2**40),
    methods=st.none() | st.lists(st.lists(_method, max_size=3).map(tuple), max_size=3).map(tuple),
)
def test_round_trip_property(sym_key, expiry, methods):
    uri = PairingURI(
        topic=hashlib.sha256(sym_key).hexdigest(),
        sym_key=sym_key.hex(),
        expiry_timestamp=expiry,
        methods=methods,
    )
    assert PairingURI.parse(uri.format()) == uri
