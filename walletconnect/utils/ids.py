"""JSON-RPC payload IDs, matching ``@walletconnect/jsonrpc-utils`` ``payloadId()``."""

import secrets
import threading
import time

DEFAULT_ENTROPY = 3

_lock = threading.Lock()
_last_ids: dict[int, int] = {}  # entropy -> last id issued


def payload_id(entropy: int = DEFAULT_ENTROPY) -> int:
    """
    Generate a JSON-RPC id: a millisecond epoch timestamp followed by ``entropy`` random digits.

    With the default entropy (3) this is a ~16-digit integer that fits in
    JavaScript ``Number.MAX_SAFE_INTEGER``, matching ``@walletconnect/jsonrpc-utils``.
    Use entropy=6 only when talking to peers that accept BigInt-encoded ids.

    Ids are strictly increasing within the process. Clients key in-flight requests by id, so
    two calls in the same millisecond must never collide (with 3 random digits a collision
    is otherwise a ~1-in-1000 event per pair, which silently strands one request until it
    times out).
    """
    if entropy < 0:
        raise ValueError("entropy must be non-negative")

    timestamp_ms = time.time_ns() // 1_000_000
    candidate = timestamp_ms * 10**entropy + secrets.randbelow(10**entropy)
    with _lock:
        last = _last_ids.get(entropy, 0)
        if candidate <= last:
            candidate = last + 1
        _last_ids[entropy] = candidate
    return candidate
