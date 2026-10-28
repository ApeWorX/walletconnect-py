"""
Protocol constants for the WalletConnect v2 (Reown) Sign API.

References:
    - Relay auth/RPC: https://specs.walletconnect.com/2.0/specs/servers/relay/relay-server-rpc
    - Sign RPC methods: https://specs.walletconnect.com/2.0/specs/clients/sign/rpc-methods
    - Pairing RPC methods: https://specs.walletconnect.com/2.0/specs/clients/core/pairing/rpc-methods
"""

from dataclasses import dataclass
from typing import Final

# Relay
DEFAULT_RELAY_URL: Final = "wss://relay.walletconnect.org"
"""Default relay websocket URL (``wss://relay.walletconnect.com`` also works)."""

RELAY_PROTOCOL: Final = "irn"
"""The only relay protocol currently defined for WalletConnect v2."""

USER_AGENT: Final = "walletconnect-py/0.x"
"""Sent as the ``ua`` query param on the relay WebSocket."""

PROJECT_ID_ENV_VAR: Final = "WALLETCONNECT_PROJECT_ID"
"""Environment variable used as a fallback source for the Reown project ID."""

PROJECT_ID_URL: Final = "https://cloud.reown.com"
"""Where users create a (free) project ID."""

# Protocol version (as used in the pairing URI, e.g. ``wc:<topic>@2?...``)
PROTOCOL_VERSION: Final = 2

# Time helpers (seconds)
THIRTY_SECONDS: Final = 30
FIVE_MINUTES: Final = 5 * 60
ONE_DAY: Final = 24 * 60 * 60
SEVEN_DAYS: Final = 7 * ONE_DAY
THIRTY_DAYS: Final = 30 * ONE_DAY

PAIRING_PROPOSAL_EXPIRY: Final = FIVE_MINUTES
"""How long an un-activated pairing (fresh ``wc:`` URI) stays valid."""

PAIRING_EXPIRY: Final = THIRTY_DAYS
"""How long an activated pairing stays valid."""

SESSION_EXPIRY: Final = SEVEN_DAYS
"""Default lifetime of a settled session."""

# Request expiry bounds for ``wc_sessionRequest`` (when ``expiry`` is set)
MIN_REQUEST_EXPIRY: Final = FIVE_MINUTES
MAX_REQUEST_EXPIRY: Final = SEVEN_DAYS


@dataclass(frozen=True)
class RpcOptions:
    """Relay publish options (``ttl`` seconds and ``tag``) for one message kind."""

    ttl: int
    tag: int


@dataclass(frozen=True)
class RpcMethodOptions:
    """Publish options for a peer JSON-RPC method's request and response."""

    req: RpcOptions
    res: RpcOptions
    reject: RpcOptions | None = None
    auto_reject: RpcOptions | None = None


PAIRING_RPC_OPTS: Final[dict[str, RpcMethodOptions]] = {
    "wc_pairingDelete": RpcMethodOptions(
        req=RpcOptions(ttl=ONE_DAY, tag=1000),
        res=RpcOptions(ttl=ONE_DAY, tag=1001),
    ),
    "wc_pairingPing": RpcMethodOptions(
        req=RpcOptions(ttl=THIRTY_SECONDS, tag=1002),
        res=RpcOptions(ttl=THIRTY_SECONDS, tag=1003),
    ),
}

SIGN_RPC_OPTS: Final[dict[str, RpcMethodOptions]] = {
    "wc_sessionPropose": RpcMethodOptions(
        req=RpcOptions(ttl=FIVE_MINUTES, tag=1100),
        res=RpcOptions(ttl=FIVE_MINUTES, tag=1101),
        reject=RpcOptions(ttl=FIVE_MINUTES, tag=1120),
        auto_reject=RpcOptions(ttl=FIVE_MINUTES, tag=1121),
    ),
    "wc_sessionSettle": RpcMethodOptions(
        req=RpcOptions(ttl=FIVE_MINUTES, tag=1102),
        res=RpcOptions(ttl=FIVE_MINUTES, tag=1103),
    ),
    "wc_sessionUpdate": RpcMethodOptions(
        req=RpcOptions(ttl=ONE_DAY, tag=1104),
        res=RpcOptions(ttl=ONE_DAY, tag=1105),
    ),
    "wc_sessionExtend": RpcMethodOptions(
        req=RpcOptions(ttl=ONE_DAY, tag=1106),
        res=RpcOptions(ttl=ONE_DAY, tag=1107),
    ),
    "wc_sessionRequest": RpcMethodOptions(
        req=RpcOptions(ttl=FIVE_MINUTES, tag=1108),
        res=RpcOptions(ttl=FIVE_MINUTES, tag=1109),
    ),
    "wc_sessionEvent": RpcMethodOptions(
        req=RpcOptions(ttl=FIVE_MINUTES, tag=1110),
        res=RpcOptions(ttl=FIVE_MINUTES, tag=1111),
    ),
    "wc_sessionDelete": RpcMethodOptions(
        req=RpcOptions(ttl=ONE_DAY, tag=1112),
        res=RpcOptions(ttl=ONE_DAY, tag=1113),
    ),
    "wc_sessionPing": RpcMethodOptions(
        req=RpcOptions(ttl=THIRTY_SECONDS, tag=1114),
        res=RpcOptions(ttl=THIRTY_SECONDS, tag=1115),
    ),
}

# Methods advertised in a pairing URI (``methods=[...]``), grouped by protocol.
PAIRING_URI_METHODS: Final[tuple[tuple[str, ...], ...]] = (("wc_sessionPropose",),)

# EIP-155 (EVM) namespace defaults
EIP155_NAMESPACE: Final = "eip155"

DEFAULT_EIP155_METHODS: Final[tuple[str, ...]] = (
    "eth_sendTransaction",
    "personal_sign",
    "eth_signTypedData_v4",
)
"""Methods requested by default; supported by essentially every WalletConnect wallet."""

OPTIONAL_EIP155_METHODS: Final[tuple[str, ...]] = (
    "eth_signTransaction",
    "wallet_switchEthereumChain",
    "wallet_addEthereumChain",
)
"""Methods worth requesting, but which many wallets do not support."""

DEFAULT_EIP155_EVENTS: Final[tuple[str, ...]] = ("chainChanged", "accountsChanged")
