"""JSON-RPC payload IDs, matching ``@walletconnect/jsonrpc-utils`` ``payloadId()``."""

import secrets
import time

DEFAULT_ENTROPY = 3


def payload_id(entropy: int = DEFAULT_ENTROPY) -> int:
    """
    Generate a JSON-RPC id: a millisecond epoch timestamp followed by ``entropy`` random digits.

    With the default entropy (3) this is a ~16-digit integer that fits in
    JavaScript ``Number.MAX_SAFE_INTEGER``, matching ``@walletconnect/jsonrpc-utils``.
    Use entropy=6 only when talking to peers that accept BigInt-encoded ids.
    """
    if entropy < 0:
        raise ValueError("entropy must be non-negative")

    timestamp_ms = time.time_ns() // 1_000_000
    return timestamp_ms * 10**entropy + secrets.randbelow(10**entropy)
