import json

from walletconnect.crypto.codec import decode_jsonrpc, encode_jsonrpc
from walletconnect.crypto.envelope import decrypt
from walletconnect.utils.ids import payload_id
from walletconnect.utils.rpc_id import JS_MAX_SAFE_INTEGER, coerce_rpc_id, encode_rpc_id


def test_coerce_bigint_string():
    assert coerce_rpc_id("1793198341137691020n") == 1793198341137691020
    assert coerce_rpc_id("42") == 42
    assert coerce_rpc_id(42) == 42


def test_encode_stays_js_safe_number():
    assert encode_rpc_id(42) == 42
    safe = JS_MAX_SAFE_INTEGER
    assert encode_rpc_id(safe) == safe


def test_payload_id_is_js_safe():
    for _ in range(20):
        assert abs(payload_id()) <= JS_MAX_SAFE_INTEGER


def test_codec_round_trips_id_as_number():
    sym = "ab" * 32
    rid = payload_id()
    payload = {"id": rid, "jsonrpc": "2.0", "method": "wc_sessionPing", "params": {}}
    env = encode_jsonrpc(payload, sym_key=sym)
    plain = json.loads(decrypt(sym_key=sym, encoded=env))
    assert plain["id"] == rid
    assert isinstance(plain["id"], int)
    decoded = decode_jsonrpc(env, sym_key=sym)
    assert coerce_rpc_id(decoded["id"]) == rid
