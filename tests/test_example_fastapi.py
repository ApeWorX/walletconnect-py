"""Smoke test for examples/fastapi_app against the in-process fake relay."""

from __future__ import annotations

import os
import stat
import sys
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from examples.fastapi_app.app import STATE_FILE_ENV, create_app  # noqa: E402
from tests.relay_thread import relay_thread  # noqa: E402
from tests.test_sign_client import scripted_wallet  # noqa: E402
from walletconnect import ClientState  # noqa: E402
from walletconnect.sign import SignClient  # noqa: E402

TYPED = {
    "types": {"EIP712Domain": [{"name": "name", "type": "string"}]},
    "primaryType": "EIP712Domain",
    "domain": {"name": "test"},
    "message": {},
}


def _wait_settled(http: TestClient, pairing_topic: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = http.get(f"/pairings/{pairing_topic}").json()
        if status["status"] != "pending":
            return status
        time.sleep(0.05)
    raise AssertionError("pairing did not settle")


def test_fastapi_example_end_to_end(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv("WALLETCONNECT_PROJECT_ID", "test")
    with relay_thread() as relay:
        monkeypatch.setenv("WALLETCONNECT_RELAY_URL", relay.url)
        with TestClient(create_app()) as http:
            assert http.get("/sessions").json() == []

            pair = http.post("/pair", params={"qr": "svg"}).json()
            assert pair["uri"].startswith("wc:")
            assert pair["qr_svg"].startswith("<svg")
            wallet = relay.submit(scripted_wallet(relay.url, pair["uri"], n_requests=2))

            status = _wait_settled(http, pair["pairing_topic"])
            assert status["status"] == "settled", status
            topic = status["session_topic"]

            sessions = http.get("/sessions").json()
            assert [s["topic"] for s in sessions] == [topic]
            assert sessions[0]["peer_name"] == "FakeWallet"

            r = http.post(f"/sessions/{topic}/sign", json={"message": "hello"})
            assert r.status_code == 200, r.text
            assert r.json() == {"signature": "0xdeadbeef"}
            r = http.post(
                f"/sessions/{topic}/sign",
                json={"method": "eth_signTypedData_v4", "typed_data": TYPED},
            )
            assert r.json() == {"signature": "0xdeadbeef"}
            wallet.result(timeout=10)

            assert http.post(f"/sessions/{topic}/sign", json={}).status_code == 422
            assert http.post("/sessions/nope/sign", json={"message": "x"}).status_code == 404

            # The session was persisted to the explicit state file (0600).
            assert topic in ClientState.load(state_file).sessions
            if sys.platform != "win32":
                assert stat.S_IMODE(os.stat(state_file).st_mode) == 0o600

        # Restart: the session comes back from the state file.
        with TestClient(create_app()) as http:
            assert [s["topic"] for s in http.get("/sessions").json()] == [topic]
            assert http.delete(f"/sessions/{topic}").status_code == 204
            assert http.get("/sessions").json() == []
            assert http.delete(f"/sessions/{topic}").status_code == 404
        assert ClientState.load(state_file).sessions == {}


def test_fastapi_example_injected_client(monkeypatch):
    monkeypatch.delenv(STATE_FILE_ENV, raising=False)
    with relay_thread() as relay:
        app = create_app(lambda: SignClient(project_id="test", relay_url=relay.url))
        with TestClient(app) as http:
            assert http.get("/pairings/unknown").status_code == 404
            assert http.get("/sessions").json() == []
