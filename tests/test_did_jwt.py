import json

import pytest

from walletconnect.crypto.did import decode_did_key, encode_did_key
from walletconnect.crypto.jwt import (
    CLIENT_AUTH_ACT,
    AuthKeyPair,
    InvalidJWTError,
    decode_jwt,
    generate_auth_key_pair,
    sign_jwt,
    verify_jwt,
)
from walletconnect.exceptions import InvalidKeyError

# Spec test vector: https://specs.walletconnect.com/2.0/specs/clients/core/crypto/crypto-authentication
SEED = "58e0254c211b858ef7896b00e3f36beeb13d568d47c6031c4218b87718061295"
PUBLIC = "884ab67f787b69e534bfdba8d5beb4e719700e90ac06317ed177d49e5a33be5a"
DID = "did:key:z6MkodHZwneVRShtaLf8JKYkxpDGp1vGZnpGmdBpX8M2exxH"
SUB = "c479fe5dc464e771e78b193d239a65b58d278cad1c34bfb0b5716e5bb514928e"
AUD = "wss://relay.walletconnect.com"
IAT = 1656910097
# Spec expected JWT has no `act` claim (older vector). We still verify did:key + signature.
SPEC_JWT = (
    "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJkaWQ6a2V5Ono2TWtvZEhad25lVlJTaHRhTGY4SktZa3hwREdwMXZHWm5wR21kQnBYOE0yZXh4SCIs"
    "InN1YiI6ImM0NzlmZTVkYzQ2NGU3NzFlNzhiMTkzZDIzOWE2NWI1OGQyNzhjYWQxYzM0YmZiMGI1NzE2ZTViYjUx"
    "NDkyOGUiLCJhdWQiOiJ3c3M6Ly9yZWxheS53YWxsZXRjb25uZWN0LmNvbSIsImlhdCI6MTY1NjkxMDA5NywiZXhw"
    "IjoxNjU2OTk2NDk3fQ."
    "bAKl1swvwqqV_FgwvD4Bx3Yp987B9gTpZctyBviA-EkAuWc8iI8SyokOjkv9GJESgid4U8Tf2foCgrQp2qrxBA"
)


def test_encode_did_key_matches_spec():
    assert encode_did_key(PUBLIC) == DID
    assert encode_did_key(bytes.fromhex(PUBLIC)) == DID
    assert decode_did_key(DID).hex() == PUBLIC


def test_generate_from_seed():
    pair = generate_auth_key_pair(SEED)
    assert pair.public_key == PUBLIC
    assert pair.private_key == SEED
    assert pair.did_key == DID


def test_sign_jwt_includes_client_auth_and_verifies():
    pair = generate_auth_key_pair(SEED)
    jwt = sign_jwt(aud=AUD, key_pair=pair, sub=SUB, ttl=86400, iat=IAT)
    payload = verify_jwt(jwt, now=IAT)
    assert payload["act"] == CLIENT_AUTH_ACT
    assert payload["iss"] == DID
    assert payload["sub"] == SUB
    assert payload["aud"] == AUD
    assert payload["iat"] == IAT
    assert payload["exp"] == IAT + 86400
    # Claim order on the wire matches the reference implementation.
    raw_payload = json.loads(
        __import__("base64").urlsafe_b64decode(jwt.split(".")[1] + "==").decode()
    )
    assert list(raw_payload) == ["iat", "exp", "act", "iss", "sub", "aud"]


def test_verify_rejects_tampered_and_expired():
    pair = generate_auth_key_pair(SEED)
    jwt = sign_jwt(aud=AUD, key_pair=pair, sub=SUB, ttl=10, iat=IAT)
    with pytest.raises(InvalidJWTError, match="expired"):
        verify_jwt(jwt, now=IAT + 11)
    header, payload, sig = jwt.split(".")
    tampered = f"{header}.{payload}.{sig[:-2]}aa"
    with pytest.raises(InvalidJWTError, match="signature"):
        verify_jwt(tampered, now=IAT)


def test_spec_jwt_signature_still_verifies_without_act():
    # The published spec vector omits `act`; our verifier must still accept it.
    parts = decode_jwt(SPEC_JWT)
    assert parts["payload"]["iss"] == DID
    assert "act" not in parts["payload"]
    payload = verify_jwt(SPEC_JWT, now=IAT)
    assert payload["sub"] == SUB


def test_random_key_pair():
    a = generate_auth_key_pair()
    b = generate_auth_key_pair()
    assert isinstance(a, AuthKeyPair)
    assert a != b
    assert a.did_key.startswith("did:key:z")


@pytest.mark.parametrize("bad", ["did:pkh:x", "did:key:xabc", "did:key:z"])
def test_decode_did_rejects(bad):
    with pytest.raises(InvalidKeyError):
        decode_did_key(bad)
