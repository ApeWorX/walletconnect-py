"""
Core protocol data models shared by the pairing and sign layers.

Inbound models (anything a wallet sends us) ignore unknown fields so that newer wallets keep
working; outbound serialization should use :meth:`WalletConnectModel.to_json`.
"""

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .constants import DEFAULT_EIP155_EVENTS, DEFAULT_EIP155_METHODS, RELAY_PROTOCOL
from .utils.caip import NAMESPACE_PATTERN, AccountId, ChainId


class WalletConnectModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    def to_json(self) -> dict[str, Any]:
        """Serialize to the JSON-compatible dict used on the wire (camelCase, no nulls)."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class Redirect(WalletConnectModel):
    """Deep-link info for returning to the app."""

    native: str | None = None
    universal: str | None = None


class Metadata(WalletConnectModel):
    """App or wallet metadata shown to the other peer."""

    name: str
    description: str = ""
    url: str = ""
    icons: list[str] = Field(default_factory=list)
    redirect: Redirect | None = None


class Relay(WalletConnectModel):
    """Relay protocol options."""

    protocol: str = RELAY_PROTOCOL
    data: str | None = None


class ProposalNamespace(WalletConnectModel):
    """A namespace in ``requiredNamespaces`` / ``optionalNamespaces`` of a session proposal."""

    chains: list[str] | None = None
    methods: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)

    @field_validator("chains")
    @classmethod
    def _validate_chains(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            for chain in value:
                ChainId.parse(chain)

        return value

    @classmethod
    def eip155(
        cls,
        chain_ids: Iterable[int],
        methods: Iterable[str] = DEFAULT_EIP155_METHODS,
        events: Iterable[str] = DEFAULT_EIP155_EVENTS,
    ) -> "ProposalNamespace":
        """Build an ``eip155`` namespace from integer EVM chain IDs."""
        return cls(
            chains=[str(ChainId.eip155(c)) for c in chain_ids],
            methods=list(methods),
            events=list(events),
        )


class SessionNamespace(WalletConnectModel):
    """A namespace in a settled session (``wc_sessionSettle`` / ``wc_sessionUpdate``)."""

    chains: list[str] | None = None
    accounts: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)

    @field_validator("chains")
    @classmethod
    def _validate_chains(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            for chain in value:
                ChainId.parse(chain)

        return value

    @field_validator("accounts")
    @classmethod
    def _validate_accounts(cls, value: list[str]) -> list[str]:
        for account in value:
            AccountId.parse(account)

        return value

    @property
    def account_ids(self) -> list[AccountId]:
        return [AccountId.parse(a) for a in self.accounts]

    @property
    def chain_ids(self) -> list[ChainId]:
        """Chains from ``chains`` if given, else derived from ``accounts`` (de-duplicated)."""
        if self.chains is not None:
            return [ChainId.parse(c) for c in self.chains]

        return list(dict.fromkeys(a.chain_id for a in self.account_ids))


def validate_namespace_key(key: str) -> str:
    """Validate a namespace key (e.g. ``eip155``), allowing a chain-scoped key (``eip155:1``)."""
    if NAMESPACE_PATTERN.match(key):
        return key

    # NOTE: CAIP-25 allows chain-specific keys like ``eip155:10``.
    return str(ChainId.parse(key))


class Participant(WalletConnectModel):
    """A peer's public key and metadata (``proposer`` / ``controller``)."""

    public_key: str = Field(alias="publicKey")
    metadata: Metadata
