import time

import pytest

from walletconnect.utils.ids import payload_id


def test_payload_id_is_19_digits():
    assert len(str(payload_id())) == 19


def test_payload_id_embeds_timestamp():
    before = time.time_ns() // 1_000_000
    pid = payload_id()
    after = time.time_ns() // 1_000_000
    assert before <= pid // 10**6 <= after


def test_payload_ids_are_unique_enough():
    assert len({payload_id() for _ in range(1000)}) > 990


def test_custom_entropy():
    assert len(str(payload_id(entropy=3))) == 16
    with pytest.raises(ValueError, match="non-negative"):
        payload_id(entropy=-1)
