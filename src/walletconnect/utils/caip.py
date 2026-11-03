"""
CAIP-2 chain IDs and CAIP-10 account IDs.

- CAIP-2: https://github.com/ChainAgnostic/CAIPs/blob/main/CAIPs/caip-2.md
- CAIP-10: https://github.com/ChainAgnostic/CAIPs/blob/main/CAIPs/caip-10.md
"""

import re
from dataclasses import dataclass

from ..constants import EIP155_NAMESPACE
from ..exceptions import InvalidCAIPIdentifierError

_NAMESPACE = r"[-a-z0-9]{3,8}"
_REFERENCE = r"[-_a-zA-Z0-9]{1,32}"
_ADDRESS = r"[-.%a-zA-Z0-9]{1,128}"

CHAIN_ID_PATTERN = re.compile(rf"^(?P<namespace>{_NAMESPACE}):(?P<reference>{_REFERENCE})$")
ACCOUNT_ID_PATTERN = re.compile(
    rf"^(?P<namespace>{_NAMESPACE}):(?P<reference>{_REFERENCE}):(?P<address>{_ADDRESS})$"
)
NAMESPACE_PATTERN = re.compile(rf"^{_NAMESPACE}$")


@dataclass(frozen=True, order=True)
class ChainId:
    """A CAIP-2 chain ID, e.g. ``eip155:1``."""

    namespace: str
    reference: str

    def __post_init__(self) -> None:
        if not CHAIN_ID_PATTERN.match(f"{self.namespace}:{self.reference}"):
            raise InvalidCAIPIdentifierError(
                f"Invalid CAIP-2 chain ID: '{self.namespace}:{self.reference}'."
            )

    @classmethod
    def parse(cls, value: str) -> "ChainId":
        if not (match := CHAIN_ID_PATTERN.match(value)):
            raise InvalidCAIPIdentifierError(f"Invalid CAIP-2 chain ID: '{value}'.")

        return cls(namespace=match["namespace"], reference=match["reference"])

    @classmethod
    def eip155(cls, chain_id: int) -> "ChainId":
        """Build an EVM chain ID, e.g. ``ChainId.eip155(1)`` -> ``eip155:1``."""
        if chain_id < 0:
            raise InvalidCAIPIdentifierError(f"Invalid EVM chain ID: {chain_id}.")

        return cls(namespace=EIP155_NAMESPACE, reference=str(chain_id))

    @property
    def is_eip155(self) -> bool:
        return self.namespace == EIP155_NAMESPACE

    @property
    def evm_chain_id(self) -> int:
        """The integer chain ID (only valid for the ``eip155`` namespace)."""
        if not self.is_eip155:
            raise InvalidCAIPIdentifierError(f"'{self}' is not an EVM (eip155) chain.")

        return int(self.reference)

    def __str__(self) -> str:
        return f"{self.namespace}:{self.reference}"


@dataclass(frozen=True, order=True)
class AccountId:
    """A CAIP-10 account ID, e.g. ``eip155:1:0xab16a96D359eC26a11e2C2b3d8f8B8942d5Bfcdb``."""

    chain_id: ChainId
    address: str

    def __post_init__(self) -> None:
        if not ACCOUNT_ID_PATTERN.match(f"{self.chain_id}:{self.address}"):
            raise InvalidCAIPIdentifierError(
                f"Invalid CAIP-10 account ID: '{self.chain_id}:{self.address}'."
            )

    @classmethod
    def parse(cls, value: str) -> "AccountId":
        if not (match := ACCOUNT_ID_PATTERN.match(value)):
            raise InvalidCAIPIdentifierError(f"Invalid CAIP-10 account ID: '{value}'.")

        chain_id = ChainId(namespace=match["namespace"], reference=match["reference"])
        return cls(chain_id=chain_id, address=match["address"])

    @property
    def namespace(self) -> str:
        return self.chain_id.namespace

    def __str__(self) -> str:
        return f"{self.chain_id}:{self.address}"
