import pytest
from pydantic import ValidationError

from walletconnect.constants import DEFAULT_EIP155_EVENTS, DEFAULT_EIP155_METHODS
from walletconnect.types import (
    Metadata,
    Participant,
    ProposalNamespace,
    Relay,
    SessionNamespace,
    validate_namespace_key,
)
from walletconnect.utils.caip import ChainId

ADDRESS = "0xab16a96D359eC26a11e2C2b3d8f8B8942d5Bfcdb"


def test_metadata_to_json_omits_nulls():
    metadata = Metadata(name="Ape", url="https://apeworx.io")
    assert metadata.to_json() == {
        "name": "Ape",
        "description": "",
        "url": "https://apeworx.io",
        "icons": [],
    }


def test_metadata_ignores_unknown_fields():
    metadata = Metadata.model_validate({"name": "Wallet", "verifyUrl": "x", "icons": ["i"]})
    assert metadata.name == "Wallet"
    assert "verifyUrl" not in metadata.to_json()


def test_relay_default():
    assert Relay().to_json() == {"protocol": "irn"}


def test_proposal_namespace_eip155():
    ns = ProposalNamespace.eip155([1, 8453])
    assert ns.to_json() == {
        "chains": ["eip155:1", "eip155:8453"],
        "methods": list(DEFAULT_EIP155_METHODS),
        "events": list(DEFAULT_EIP155_EVENTS),
    }


def test_proposal_namespace_invalid_chain():
    with pytest.raises(ValidationError):
        ProposalNamespace(chains=["nope"])


def test_session_namespace():
    ns = SessionNamespace.model_validate(
        {
            "accounts": [f"eip155:1:{ADDRESS}", f"eip155:10:{ADDRESS}", f"eip155:1:{ADDRESS}"],
            "methods": ["personal_sign"],
            "events": [],
        }
    )
    assert ns.chain_ids == [ChainId.eip155(1), ChainId.eip155(10)]
    assert ns.account_ids[1].chain_id == ChainId.eip155(10)


def test_session_namespace_explicit_chains():
    ns = SessionNamespace(chains=["eip155:5"], accounts=[f"eip155:1:{ADDRESS}"])
    assert ns.chain_ids == [ChainId.eip155(5)]


def test_session_namespace_invalid_account():
    with pytest.raises(ValidationError):
        SessionNamespace(accounts=["eip155:1"])


def test_participant_alias():
    data = {"publicKey": "ab" * 32, "metadata": {"name": "Ape"}}
    participant = Participant.model_validate(data)
    assert participant.public_key == "ab" * 32
    assert participant.to_json()["publicKey"] == "ab" * 32
    # populate_by_name also works
    assert Participant(public_key="00", metadata=Metadata(name="x")).public_key == "00"


@pytest.mark.parametrize("key", ["eip155", "eip155:10", "solana"])
def test_validate_namespace_key(key):
    assert validate_namespace_key(key) == key


def test_validate_namespace_key_invalid():
    with pytest.raises(ValueError, match="Invalid CAIP-2"):
        validate_namespace_key("E!")
