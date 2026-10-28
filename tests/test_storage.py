from walletconnect.storage import KeyValueStorage


def test_kv_round_trip(tmp_path):
    store = KeyValueStorage(tmp_path)
    assert store.get("missing") is None
    store.set("client_seed", "ab" * 32)
    assert store.get("client_seed") == "ab" * 32
    assert store.has("client_seed")
    assert "client_seed" in store.keys()  # noqa: SIM118
    store.delete("client_seed")
    assert not store.has("client_seed")
