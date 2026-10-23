"""JSON-RPC payload IDs, matching ``@walletconnect/jsonrpc-utils`` ``payloadId()``."""

import secrets
import time

DEFAULT_ENTROPY = 6


def payload_id(entropy: int = DEFAULT_ENTROPY) -> int:
    """
    Generate a JSON-RPC id: a millisecond epoch timestamp followed by ``entropy`` random digits.

    With the default entropy this is a 19-digit integer, as expected by the relay.
    """
    if entropy < 0:
        raise ValueError("entropy must be non-negative")

    timestamp_ms = time.time_ns() // 1_000_000
    return timestamp_ms * 10**entropy + secrets.randbelow(10**entropy)
