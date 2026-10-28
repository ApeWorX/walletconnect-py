"""
Exceptions raised by ``walletconnect``.

Peer (wallet) errors use the WalletConnect SDK error codes:
https://specs.walletconnect.com/2.0/specs/clients/sign/error-codes
"""

from typing import Any, ClassVar

from .constants import PROJECT_ID_ENV_VAR, PROJECT_ID_URL


class WalletConnectError(Exception):
    """Base class for all errors raised by this library."""


class MissingProjectIdError(WalletConnectError):
    """Raised when no Reown project ID was provided."""

    def __init__(self) -> None:
        super().__init__(
            "A WalletConnect (Reown) project ID is required. Create a free project at "
            f"{PROJECT_ID_URL} and pass `project_id=...` or set ${PROJECT_ID_ENV_VAR}."
        )


class InvalidCAIPIdentifierError(WalletConnectError, ValueError):
    """Raised when a CAIP-2 chain ID or CAIP-10 account ID is malformed."""


class InvalidPairingURIError(WalletConnectError, ValueError):
    """Raised when a ``wc:`` pairing URI is malformed or unsupported (e.g. WalletConnect v1)."""


class InvalidKeyError(WalletConnectError, ValueError):
    """Raised when a hex-encoded crypto key is malformed."""


class InvalidJWTError(WalletConnectError, ValueError):
    """Raised when a did-JWT is malformed or its signature does not verify."""


class InvalidEnvelopeError(WalletConnectError, ValueError):
    """Raised when a typed envelope cannot be decoded or decrypted."""


class RelayError(WalletConnectError):
    """Raised when the WalletConnect relay returns a JSON-RPC error or protocol fault."""


class RelayConnectionError(RelayError, ConnectionError):
    """Raised when the relay WebSocket cannot be established or drops unexpectedly."""


class WalletConnectTimeoutError(WalletConnectError, TimeoutError):
    """Raised when the peer or relay does not respond in time."""


class SdkError(WalletConnectError):
    """
    An error returned by the peer as a JSON-RPC error with a WalletConnect SDK error code.

    Use :meth:`SdkError.from_code` to get the most specific subclass for a given code.
    """

    code: ClassVar[int] = 0
    default_message: ClassVar[str] = "Unknown WalletConnect error"

    _registry: ClassVar[dict[int, type["SdkError"]]] = {}

    def __init__(self, message: str | None = None, data: Any = None, code: int | None = None):
        self.message = message or self.default_message
        self.data = data
        if code is not None:
            # NOTE: Only used for codes without a dedicated subclass.
            self.code = code  # type: ignore[misc]
        super().__init__(f"[{self.code}] {self.message}")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.code in SdkError._registry:
            raise TypeError(f"Duplicate SDK error code {cls.code} ({cls.__name__}).")
        SdkError._registry[cls.code] = cls

    @classmethod
    def from_code(cls, code: int, message: str | None = None, data: Any = None) -> "SdkError":
        """Build the most specific :class:`SdkError` for a JSON-RPC error ``code``."""
        if error_cls := SdkError._registry.get(code):
            return error_cls(message, data=data)

        return SdkError(message, data=data, code=code)

    def to_json(self) -> dict[str, Any]:
        """Serialize as a JSON-RPC ``error`` object."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data

        return error


# INVALID
class InvalidMethodError(SdkError):
    code = 1001
    default_message = "Invalid method"


class InvalidEventError(SdkError):
    code = 1002
    default_message = "Invalid event"


class InvalidUpdateRequestError(SdkError):
    code = 1003
    default_message = "Invalid update request"


class InvalidExtendRequestError(SdkError):
    code = 1004
    default_message = "Invalid extend request"


class InvalidSessionSettleRequestError(SdkError):
    code = 1005
    default_message = "Invalid session settle request"


# UNAUTHORIZED
class UnauthorizedMethodError(SdkError):
    code = 3001
    default_message = "Unauthorized method"


class UnauthorizedEventError(SdkError):
    code = 3002
    default_message = "Unauthorized event"


class UnauthorizedUpdateRequestError(SdkError):
    code = 3003
    default_message = "Unauthorized update request"


class UnauthorizedExtendRequestError(SdkError):
    code = 3004
    default_message = "Unauthorized extend request"


class UnauthorizedChainError(SdkError):
    code = 3005
    default_message = "Unauthorized chain"


# EIP-1193
class UserRejectedRequestError(SdkError):
    code = 4001
    default_message = "User rejected the request"


# REJECTED (CAIP-25)
class UserRejectedError(SdkError):
    code = 5000
    default_message = "User rejected"


class UserRejectedChainsError(SdkError):
    code = 5001
    default_message = "User rejected chains"


class UserRejectedMethodsError(SdkError):
    code = 5002
    default_message = "User rejected methods"


class UserRejectedEventsError(SdkError):
    code = 5003
    default_message = "User rejected events"


class UnsupportedChainsError(SdkError):
    code = 5100
    default_message = "Unsupported chains"


class UnsupportedMethodsError(SdkError):
    code = 5101
    default_message = "Unsupported methods"


class UnsupportedEventsError(SdkError):
    code = 5102
    default_message = "Unsupported events"


class UnsupportedAccountsError(SdkError):
    code = 5103
    default_message = "Unsupported accounts"


class UnsupportedNamespaceKeyError(SdkError):
    code = 5104
    default_message = "Unsupported namespace key"


# REASON
class UserDisconnectedError(SdkError):
    code = 6000
    default_message = "User disconnected"


# FAILURE
class SessionSettlementFailedError(SdkError):
    code = 7000
    default_message = "Session settlement failed"


class NoSessionForTopicError(SdkError):
    code = 7001
    default_message = "No session for topic"


# SESSION REQUEST
class SessionRequestExpiredError(SdkError):
    code = 8000
    default_message = "Session request expired"


USER_REJECTION_CODES: frozenset[int] = frozenset({4001, 5000, 5001, 5002, 5003})
"""Codes meaning "the user said no" (useful for mapping onto e.g. Ape's ``SignatureError``)."""
