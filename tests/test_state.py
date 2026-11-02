"""Stateless client: state models, explicit-path helpers, restore and multi-session."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import time

import pytest

from tests.fake_relay import run_fake_relay
from tests.test_sign_client import scripted_wallet
from walletconnect.constants import SIGN_RPC_OPTS
from walletconnect.crypto.codec import encode_jsonrpc
from walletconnect.crypto.envelope import TYPE_0
from walletconnect.exceptions import NoActiveSessionError, SessionExpiredError
from walletconnect.sign import ClientState, Session, SignClient
from walletconnect.types import Metadata
from walletconnect.utils.ids import payload_id

META = Metadata(name="state-test", url="https://example.com")


def _session(topic: str = "ab" * 32, *, expiry: int | None = None) -> Session:
    return Session(
        topic=topic,
        sym_key="cd" * 32,
        expiry=expiry if expiry is not None else int(time.time()) + 3600,
        namespaces={"eip155": {"accounts": ["eip155:1:0x" + "11" * 20]}},
    )


def test_state_json_round_trip():
    state = ClientState(sessions={"ab" * 32: _session()})
    blob = state.model_dump_json()
    restored = ClientState.model_validate_json(blob)
    assert restored == state
    assert restored.client_id == state.client_id
    assert restored.sessions["ab" * 32].eip155_accounts == ["eip155:1:0x" + "11" * 20]


def test_state_repr_hides_secrets():
    state = ClientState(sessions={"ab" * 32: _session()})
    assert state.relay_key not in repr(state)
    assert "cd" * 32 not in repr(state)


def test_state_validates_keys():
    with pytest.raises(ValueError, match="relay_key"):
        ClientState(relay_key="nothex")
    with pytest.raises(ValueError, match="sym_key"):
        Session(topic="x", sym_key="00", expiry=0)


def test_save_and_load_explicit_path(tmp_path):
    path = tmp_path / "nested" / "wc-state.json"
    state = ClientState(sessions={"ab" * 32: _session()})
    assert state.save(path) == path
    assert ClientState.load(path) == state
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # Overwrite atomically, no temp files left behind.
    ClientState().save(path)
    assert [p.name for p in path.parent.iterdir()] == ["wc-state.json"]


def test_prune_expired():
    live, dead = _session("aa" * 32), _session("bb" * 32, expiry=int(time.time()) - 1)
    state = ClientState(sessions={live.topic: live, dead.topic: dead})
    assert state.prune_expired() == [dead.topic]
    assert list(state.sessions) == [live.topic]


def test_client_never_touches_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    client = SignClient(project_id="test", metadata=META)
    assert client.sessions == {}
    assert client.session is None
    assert list(tmp_path.iterdir()) == []


def test_restore_drops_expired_and_copies_state():
    dead = _session("bb" * 32, expiry=int(time.time()) - 1)
    live = _session("aa" * 32)
    state = ClientState(sessions={dead.topic: dead, live.topic: live})
    client = SignClient(project_id="test", metadata=META, state=state)
    assert list(client.sessions) == [live.topic]
    assert client.client_id == state.client_id
    # Caller's object is not mutated.
    assert dead.topic in state.sessions


@pytest.mark.asyncio
async def test_expired_session_rejected_before_request():
    live = _session("aa" * 32)
    client = SignClient(
        project_id="test", metadata=META, state=ClientState(sessions={live.topic: live})
    )
    client._state.sessions[live.topic].expiry = int(time.time()) - 1
    with pytest.raises(SessionExpiredError):
        await client.request("personal_sign", [], topic=live.topic)
    assert client.sessions == {}


@pytest.mark.asyncio
async def test_restore_resumes_session_and_fetches_missed_messages():
    seen: dict = {}
    snapshots: list[ClientState] = []
    async with run_fake_relay() as (_fake, base_url):
        client = SignClient(
            project_id="test",
            relay_url=base_url,
            metadata=META,
            on_state_change=snapshots.append,
            connect_timeout=10,
            request_timeout=10,
        )
        uri, task = await client.connect()
        wallet_task = asyncio.create_task(scripted_wallet(base_url, uri, seen=seen, n_requests=2))
        session = await client.wait_for_session(task)
        assert snapshots, "state change callback fires on settle"
        assert session.topic in snapshots[-1].sessions
        address = session.eip155_accounts[0].split(":")[-1]
        assert await client.personal_sign("one", address) == "0xdeadbeef"

        blob = client.export_state().model_dump_json()
        await client.close()

        # While the dApp is offline the wallet changes accounts (wc_sessionUpdate).
        new_ns = {"eip155": {"accounts": ["eip155:1:0x" + "22" * 20], "methods": [], "events": []}}
        update = {
            "id": payload_id(),
            "jsonrpc": "2.0",
            "method": "wc_sessionUpdate",
            "params": {"namespaces": new_ns},
        }
        opts = SIGN_RPC_OPTS["wc_sessionUpdate"].req
        await seen["wallet"].publish(
            seen["session_topic"],
            encode_jsonrpc(update, sym_key=seen["sym_key"], type=TYPE_0),
            ttl=opts.ttl,
            tag=opts.tag,
        )

        restored = SignClient(
            project_id="test",
            relay_url=base_url,
            metadata=META,
            state=ClientState.model_validate_json(blob),
            connect_timeout=10,
            request_timeout=10,
        )
        assert restored.client_id == client.client_id
        await restored.open()
        assert restored.relay.subscriptions.keys() == {session.topic}
        await asyncio.sleep(0.2)
        assert restored.session is not None
        assert restored.session.eip155_accounts == ["eip155:1:0x" + "22" * 20]
        assert await restored.personal_sign("two", address) == "0xdeadbeef"
        await wallet_task
        await restored.disconnect()
        assert restored.sessions == {}
        await restored.close()


@pytest.mark.asyncio
async def test_multiple_concurrent_sessions():
    async with run_fake_relay() as (_fake, base_url):
        client = SignClient(
            project_id="test",
            relay_url=base_url,
            metadata=META,
            connect_timeout=10,
            request_timeout=10,
        )
        uri_a, task_a = await client.connect()
        uri_b, task_b = await client.connect()
        assert uri_a != uri_b
        assert len(client.pending_pairings) == 2
        wallets = [
            asyncio.create_task(
                scripted_wallet(base_url, uri_a, account="eip155:1:0x" + "aa" * 20)
            ),
            asyncio.create_task(
                scripted_wallet(base_url, uri_b, account="eip155:1:0x" + "bb" * 20)
            ),
        ]
        sa, sb = await asyncio.gather(task_a, task_b)
        assert sa.topic != sb.topic
        assert set(client.sessions) == {sa.topic, sb.topic}
        assert client.pending_pairings == []
        with pytest.raises(NoActiveSessionError, match="topic="):
            await client.personal_sign("x", "0x" + "aa" * 20)
        with pytest.raises(NoActiveSessionError):
            _ = client.session
        assert await client.personal_sign("x", "0x" + "aa" * 20, topic=sa.topic) == "0xdeadbeef"
        assert await client.personal_sign("x", "0x" + "bb" * 20, topic=sb.topic) == "0xdeadbeef"
        await asyncio.gather(*wallets)
        await client.disconnect(topic=sa.topic)
        assert list(client.sessions) == [sb.topic]
        await client.disconnect()
        assert client.sessions == {}
        await client.close()
