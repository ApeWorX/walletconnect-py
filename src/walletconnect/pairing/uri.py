"""
WalletConnect v2 pairing URIs (EIP-1328).

Spec: https://specs.walletconnect.com/2.0/specs/clients/core/pairing/pairing-uri

Example::

    wc:7f6e504bfad60b485450578e05678ed3e8e8c4751d3c6160be17160d63ec90f9@2?relay-protocol=irn&symKey=587d5484ce2a2a6ee3ba1962fdd7e8588e06200c46823bd18fbd67def96ad303&expiryTimestamp=1705667684
"""

import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote

from ..constants import PAIRING_PROPOSAL_EXPIRY, PROTOCOL_VERSION, RELAY_PROTOCOL
from ..exceptions import InvalidPairingURIError

SCHEME = "wc"

_HEX32 = re.compile(r"^[0-9a-f]{64}$")
_URI = re.compile(r"^wc:(?P<topic>[^@?]+)(?:@(?P<version>\d+))?(?:\?(?P<query>.*))?$", re.DOTALL)
_METHOD_GROUP = re.compile(r"\[([^\[\]]*)\]")

# Parameters with dedicated fields; anything else is kept in ``extra``.
_KNOWN_PARAMS = ("symKey", "relay-protocol", "relay-data", "expiryTimestamp", "methods")

# Characters left unescaped when formatting (keeps ``methods=[a,b],[c]`` readable, as in the spec).
_SAFE = "[],-_.~"

MethodGroups = tuple[tuple[str, ...], ...]


def _check_hex32(name: str, value: str) -> str:
    if not _HEX32.match(value):
        raise InvalidPairingURIError(f"'{name}' must be 32 bytes of lowercase hex, got '{value}'.")

    return value


def parse_methods(value: str) -> MethodGroups:
    """Parse ``[wc_sessionPropose],[wc_authRequest,wc_authBatchRequest]`` into groups."""
    value = value.strip()
    if not value:
        return ()

    groups = _METHOD_GROUP.findall(value)
    # Everything outside the brackets must be the `,` separators.
    if not groups or _METHOD_GROUP.sub("", value).strip(",") != "":
        raise InvalidPairingURIError(f"Invalid 'methods' parameter: '{value}'.")

    return tuple(tuple(m.strip() for m in g.split(",") if m.strip()) for g in groups)


def format_methods(methods: MethodGroups) -> str:
    return ",".join(f"[{','.join(group)}]" for group in methods)


@dataclass(frozen=True)
class PairingURI:
    """A parsed (or freshly generated) WalletConnect v2 pairing URI."""

    topic: str
    sym_key: str
    relay_protocol: str = RELAY_PROTOCOL
    relay_data: str | None = None
    expiry_timestamp: int | None = None
    methods: MethodGroups | None = None
    version: int = PROTOCOL_VERSION
    extra: dict[str, str] = field(default_factory=dict, compare=False)
    """Unknown query parameters, preserved on round-trip."""

    def __post_init__(self) -> None:
        _check_hex32("topic", self.topic)
        _check_hex32("symKey", self.sym_key)
        if self.version != PROTOCOL_VERSION:
            raise InvalidPairingURIError(
                f"Unsupported WalletConnect version {self.version}; only v2 is supported."
            )

        if not self.relay_protocol:
            raise InvalidPairingURIError("Missing 'relay-protocol'.")

    @classmethod
    def generate(
        cls,
        expiry: int | None = PAIRING_PROPOSAL_EXPIRY,
        methods: MethodGroups | None = None,
        relay_protocol: str = RELAY_PROTOCOL,
        relay_data: str | None = None,
    ) -> "PairingURI":
        """
        Create a new pairing with a random symmetric key.

        The topic is ``sha256(symKey)``, matching the reference ``@walletconnect/core`` SDK.

        Args:
            expiry: Seconds from now until the pairing proposal expires (``None`` to omit).
            methods: Optional ``methods`` groups to advertise, e.g. ``(("wc_sessionPropose",),)``.
            relay_protocol: Relay protocol name.
            relay_data: Optional relay data.
        """
        sym_key = secrets.token_bytes(32)
        return cls(
            topic=hashlib.sha256(sym_key).hexdigest(),
            sym_key=sym_key.hex(),
            relay_protocol=relay_protocol,
            relay_data=relay_data,
            expiry_timestamp=None if expiry is None else int(time.time()) + expiry,
            methods=methods,
        )

    @classmethod
    def parse(cls, uri: str) -> "PairingURI":
        """Parse a ``wc:`` URI. Raises :class:`~walletconnect.exceptions.InvalidPairingURIError`."""
        uri = uri.strip()
        if not (match := _URI.match(uri)):
            raise InvalidPairingURIError(f"Not a WalletConnect URI: '{uri}'.")

        version = int(match["version"]) if match["version"] else None
        if version is None:
            raise InvalidPairingURIError("Missing WalletConnect version (expected '@2').")

        if version == 1:
            raise InvalidPairingURIError("WalletConnect v1 URIs are not supported (v1 is dead).")

        params: dict[str, str] = {}
        for key, value in parse_qsl(match["query"] or "", keep_blank_values=True):
            if key in params:
                raise InvalidPairingURIError(f"Duplicate parameter '{key}'.")

            params[key] = value

        if "symKey" not in params:
            raise InvalidPairingURIError("Missing 'symKey'.")

        if "relay-protocol" not in params:
            raise InvalidPairingURIError("Missing 'relay-protocol'.")

        expiry: int | None = None
        if raw_expiry := params.get("expiryTimestamp"):
            if not raw_expiry.isdigit():
                raise InvalidPairingURIError(f"Invalid 'expiryTimestamp': '{raw_expiry}'.")

            expiry = int(raw_expiry)

        return cls(
            topic=match["topic"],
            version=version,
            sym_key=params["symKey"],
            relay_protocol=params["relay-protocol"],
            relay_data=params.get("relay-data") or None,
            expiry_timestamp=expiry,
            methods=parse_methods(params["methods"]) if "methods" in params else None,
            extra={k: v for k, v in params.items() if k not in _KNOWN_PARAMS},
        )

    def format(self) -> str:
        """Serialize to a ``wc:`` URI string."""
        params: list[tuple[str, str]] = [
            ("relay-protocol", self.relay_protocol),
            ("symKey", self.sym_key),
        ]
        if self.relay_data:
            params.append(("relay-data", self.relay_data))

        if self.expiry_timestamp is not None:
            params.append(("expiryTimestamp", str(self.expiry_timestamp)))

        if self.methods is not None:
            params.append(("methods", format_methods(self.methods)))

        params.extend(self.extra.items())
        query = "&".join(f"{quote(k, safe=_SAFE)}={quote(v, safe=_SAFE)}" for k, v in params)
        return f"{SCHEME}:{self.topic}@{self.version}?{query}"

    def is_expired(self, now: float | None = None) -> bool:
        """Whether ``expiryTimestamp`` has passed (URIs without an expiry never expire)."""
        if self.expiry_timestamp is None:
            return False

        return (time.time() if now is None else now) >= self.expiry_timestamp

    @property
    def sym_key_bytes(self) -> bytes:
        return bytes.fromhex(self.sym_key)

    def __str__(self) -> str:
        return self.format()
