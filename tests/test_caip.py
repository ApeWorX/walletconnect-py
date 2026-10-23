import pytest
from hypothesis import (
    given,
    strategies as st,
)

from walletconnect.exceptions import InvalidCAIPIdentifierError
from walletconnect.utils.caip import AccountId, ChainId

ADDRESS = "0xab16a96D359eC26a11e2C2b3d8f8B8942d5Bfcdb"


def test_chain_id_parse_and_str():
    chain = ChainId.parse("eip155:1")
    assert chain == ChainId("eip155", "1") == ChainId.eip155(1)
    assert str(chain) == "eip155:1"
    assert chain.is_eip155
    assert chain.evm_chain_id == 1


def test_non_evm_chain():
    chain = ChainId.parse("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
    assert not chain.is_eip155
    with pytest.raises(InvalidCAIPIdentifierError):
        _ = chain.evm_chain_id


@pytest.mark.parametrize(
    "value", ["", "eip155", "eip155:", "EIP155:1", "ab:1", "eip155:1:2", "toolongns:1"]
)
def test_chain_id_invalid(value):
    with pytest.raises(InvalidCAIPIdentifierError):
        ChainId.parse(value)


def test_invalid_chain_id_is_value_error():
    with pytest.raises(ValueError, match="Invalid CAIP-2"):
        ChainId("x", "1")

    with pytest.raises(InvalidCAIPIdentifierError):
        ChainId.eip155(-1)


def test_account_id():
    account = AccountId.parse(f"eip155:137:{ADDRESS}")
    assert account.chain_id == ChainId.eip155(137)
    assert account.address == ADDRESS
    assert account.namespace == "eip155"
    assert str(account) == f"eip155:137:{ADDRESS}"


@pytest.mark.parametrize("value", ["eip155:1", f"eip155::{ADDRESS}", "eip155:1:", "eip155:1:a b"])
def test_account_id_invalid(value):
    with pytest.raises(InvalidCAIPIdentifierError):
        AccountId.parse(value)


@given(st.integers(min_value=0, max_value=2**64))
def test_eip155_round_trip(chain_id):
    chain = ChainId.eip155(chain_id)
    assert ChainId.parse(str(chain)).evm_chain_id == chain_id
