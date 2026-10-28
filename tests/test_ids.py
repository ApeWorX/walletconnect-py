import time

import pytest

from walletconnect.utils.ids import payload_id
from walletconnect.utils.rpc_id import JS_MAX_SAFE_INTEGER


def test_payload_id_default_is_js_safe():
    pid = payload_id()
    assert abs(pid) <= JS_MAX_SAFE_INTEGER
    # ~16 digits with entropy=3 (ms timestamp + 3 random digits)
    assert 15 <= len(str(pid)) <= 16


def test_payload_id_embeds_timestamp():
    before = time.time_ns() // 1_000_000
    pid = payload_id()
    after = time.time_ns() // 1_000_000
    assert before <= pid // 10**3 <= after


def test_payload_ids_are_unique_enough():
    # entropy=3 → only 1000 values per ms; sample across time with a tiny sleep.
    ids: set[int] = set()
    for _ in range(50):
        ids.add(payload_id())
        time.sleep(0.001)
    assert len(ids) >= 45


def test_custom_entropy():
    assert len(str(payload_id(entropy=6))) == 19
    with pytest.raises(ValueError, match="entropy"):
        payload_id(entropy=-1)
