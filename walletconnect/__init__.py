"""
WalletConnect v2 (Reown) client implementation in Python.

NOTE: The protocol implementation is still in progress. See the README for project status.
"""

from importlib.metadata import (
    PackageNotFoundError as _PackageNotFoundError,
    version as _version,
)

from .constants import DEFAULT_RELAY_URL
from .exceptions import MissingProjectIdError, SdkError, WalletConnectError
from .pairing import PairingURI
from .types import Metadata, ProposalNamespace, SessionNamespace
from .utils.caip import AccountId, ChainId

try:
    __version__ = _version(__name__)
except _PackageNotFoundError:
    # package is not installed
    __version__ = "<unknown>"

__all__ = [
    "DEFAULT_RELAY_URL",
    "AccountId",
    "ChainId",
    "Metadata",
    "MissingProjectIdError",
    "PairingURI",
    "ProposalNamespace",
    "SdkError",
    "SessionNamespace",
    "WalletConnectError",
    "__version__",
]
