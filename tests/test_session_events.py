"""Long-running session lifecycle: events, expiry, extend, auto-extend, reachability."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

import pytest

from tests.fake_relay import run_fake_relay
from tests.fake_wallet import NOT_ANSWERED, FakeWallet
from tests.relay_thread import relay_thread
from walletconnect import ClientState, Metadata
from walletconnect.constants import SESSION_EXPIRY
from walletconnect.exceptions import (
    NoActiveSessionError,
    SessionDeletedError,
    SessionExpiredError,
    UserRejectedError,
    WalletConnectTimeoutError,
)
from walletconnect.sign import ClientEvent, SignClient, SyncSignClient

META = Metadata(name="events-test", description="", url="https://example.com", icons=[])
WEEK_FROM_NOW = pytest.approx(int(time.time()) + SESSION_EXPIRY, abs=120)


async def _until(predicate, timeout: float = 5.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


@contextlib.asynccontextmanager
async def paired(
    *, wallet_kwargs: dict | None = None, **client_kwargs
) -> AsyncIterator[tuple[SignClient, FakeWallet, list[ClientEvent], object]]:
    """Yield ``(client, wallet, events, fake_relay)`` with one settled session."""
    async with run_fake_relay() as (fake, url):
        options = {"connect_timeout": 10, "request_timeout": 10, **client_kwargs}
        client = SignClient(project_id="test", relay_url=url, metadata=META, **options)
        events: list[ClientEvent] = []
        client.on("*", events.append)
        uri, task = await client.connect()
        wallet = await FakeWallet(url, uri, **(wallet_kwargs or {})).start()
        await client.wait_for_session(task)
        try:
            yield client, wallet, events, fake
        finally:
            await wallet.close()
            await client.close()


def _names(events: list[ClientEvent]) -> list[str]:
    return [e.name for e in events]


async def test_on_validates_names_and_works_as_decorator():
    client = SignClient(project_id="test", metadata=META)
    with pytest.raises(ValueError, match="Unknown event"):
        client.on("session_updated", print)

    @client.on("session_delete")
    def handler(event):  # pragma: no cover - not fired here
        pass

    assert client._handlers["session_delete"] == [handler]
    client.off("session_delete", handler)
    client.off("session_delete", handler)  # no-op
    assert client._handlers["session_delete"] == []


async def test_session_update_from_controller_replaces_namespaces():
    async with paired() as (client, wallet, events, _):
        new_ns = {"eip155": {"accounts": ["eip155:10:0x" + "22" * 20], "methods": [], "events": []}}
        res = await wallet.send("wc_sessionUpdate", {"namespaces": new_ns})
        assert res["result"] is True
        await _until(lambda: "session_update" in _names(events))
        session = client.sessions[wallet.topic]
        assert session.namespaces == new_ns
        update = next(e for e in events if e.name == "session_update")
        assert update.topic == wallet.topic
        assert update.session is not None
        assert update.session.namespaces == new_ns
        assert "sym_key" not in str(update.to_dict())

        # Same id again (redelivery after a restart): acknowledged, not re-applied.
        n_events = len(events)
        again = await wallet.send(
            "wc_sessionUpdate", {"namespaces": new_ns}, request_id=session.last_update_id
        )
        assert again["result"] is True
        # Older id: rejected, namespaces unchanged.
        stale = await wallet.send(
            "wc_sessionUpdate",
            {"namespaces": {"eip155": {"accounts": [], "methods": [], "events": []}}},
            request_id=session.last_update_id - 1000,
        )
        assert stale["error"]["code"] == 1003
        # Empty namespaces: rejected.
        empty = await wallet.send("wc_sessionUpdate", {"namespaces": {}})
        assert empty["error"]["code"] == 1003
        await asyncio.sleep(0.05)
        assert len(events) == n_events
        assert client.sessions[wallet.topic].namespaces == new_ns
        # The newest id is persisted so a restart still rejects stale updates.
        assert client.export_state().sessions[wallet.topic].last_update_id == session.last_update_id


async def test_session_update_from_non_controller_is_rejected():
    other = "ab" * 32
    async with paired(wallet_kwargs={"controller_key": other}) as (client, wallet, events, _):
        before = client.sessions[wallet.topic].namespaces
        res = await wallet.send(
            "wc_sessionUpdate",
            {"namespaces": {"eip155": {"accounts": [], "methods": [], "events": []}}},
        )
        assert res["error"]["code"] == 3003
        assert client.sessions[wallet.topic].namespaces == before
        assert "session_update" not in _names(events)


async def test_session_event_is_a_hint_only():
    async with paired() as (client, wallet, events, _):
        before = client.sessions[wallet.topic].namespaces
        res = await wallet.send(
            "wc_sessionEvent",
            {
                "event": {"name": "accountsChanged", "data": ["0x" + "33" * 20]},
                "chainId": "eip155:1",
            },
        )
        assert res["result"] is True
        await _until(lambda: "session_event" in _names(events))
        event = next(e for e in events if e.name == "session_event")
        assert event.data == {
            "name": "accountsChanged",
            "data": ["0x" + "33" * 20],
            "chain_id": "eip155:1",
        }
        assert client.sessions[wallet.topic].namespaces == before


async def test_session_delete_fails_pending_requests_and_drops_session():
    snapshots: list[ClientState] = []
    async with paired(on_state_change=snapshots.append) as (client, wallet, events, _):
        wallet.answers["wc_sessionRequest"] = NOT_ANSWERED
        pending = asyncio.create_task(client.personal_sign("hi", "0x" + "11" * 20))
        await _until(lambda: wallet.requests("wc_sessionRequest"))
        res = await wallet.send("wc_sessionDelete", {"code": 6000, "message": "bye"})
        assert res["result"] is True
        with pytest.raises(SessionDeletedError, match="bye") as info:
            await asyncio.wait_for(pending, 2)
        assert info.value.code == 6000
        assert info.value.reason == "bye"
        assert client.sessions == {}
        await _until(lambda: "session_delete" in _names(events))
        deleted = next(e for e in events if e.name == "session_delete")
        assert deleted.data == {"code": 6000, "message": "bye"}
        assert deleted.session is not None
        assert deleted.session.topic == wallet.topic
        assert snapshots[-1].sessions == {}


async def test_local_expiry_timer_drops_session_and_fails_pending():
    wallet_kwargs = {"expiry": int(time.time()) + 2}
    async with paired(wallet_kwargs=wallet_kwargs) as (client, wallet, events, _):
        wallet.answers["wc_sessionRequest"] = NOT_ANSWERED
        pending = asyncio.create_task(client.personal_sign("hi", "0x" + "11" * 20))
        with pytest.raises(SessionExpiredError):
            await asyncio.wait_for(pending, 5)
        assert client._state.sessions == {}
        await _until(lambda: "session_expire" in _names(events))
        with pytest.raises(NoActiveSessionError):
            await client.request("personal_sign", [], topic=wallet.topic)


async def test_expired_on_load_emits_session_expire_on_open():
    async with paired() as (client, wallet, _events, _):
        state = client.export_state()
    state.sessions[wallet.topic].expiry = int(time.time()) - 5
    async with run_fake_relay() as (_fake, url):
        restored = SignClient(project_id="test", relay_url=url, metadata=META, state=state)
        assert restored.sessions == {}
        events: list[ClientEvent] = []
        restored.on("session_expire", events.append)
        await restored.open()
        await _until(lambda: events)
        assert events[0].topic == wallet.topic
        await restored.close()


async def test_dapp_extend_applies_only_after_wallet_confirms():
    async with paired() as (client, wallet, events, _):
        original = client.sessions[wallet.topic].expiry

        wallet.answers["wc_sessionExtend"] = NOT_ANSWERED
        with pytest.raises(WalletConnectTimeoutError):
            await client.extend(timeout=0.3)
        assert client.sessions[wallet.topic].expiry == original

        wallet.answers["wc_sessionExtend"] = {"code": 5000, "message": "no"}
        with pytest.raises(UserRejectedError):
            await client.extend(timeout=2)
        assert client.sessions[wallet.topic].expiry == original

        wallet.answers["wc_sessionExtend"] = True
        a, b = await asyncio.gather(client.extend(timeout=2), client.extend(timeout=2))
        assert a == b == WEEK_FROM_NOW
        assert client.sessions[wallet.topic].expiry == a
        assert len(wallet.requests("wc_sessionExtend")) == 3  # concurrent calls shared one
        assert "expiry" in wallet.requests("wc_sessionExtend")[-1]["params"]
        await _until(lambda: "session_extend" in _names(events))
        extended = next(e for e in events if e.name == "session_extend")
        assert extended.data == {"expiry": a, "by": "dapp"}


async def test_wallet_extend_uses_now_plus_seven_days():
    async with paired() as (client, wallet, events, _):
        res = await wallet.send("wc_sessionExtend", {"expiry": int(time.time()) + 60})
        assert res["result"] is True
        await _until(lambda: "session_extend" in _names(events))
        assert client.sessions[wallet.topic].expiry == WEEK_FROM_NOW
        assert next(e for e in events if e.name == "session_extend").data["by"] == "wallet"


async def test_auto_extend_retries_each_cycle_until_confirmed():
    wallet_kwargs = {"expiry": int(time.time()) + 3600}  # 1h left: inside both thresholds
    async with paired(
        wallet_kwargs=wallet_kwargs,
        auto_extend=True,
        maintenance_interval=0.2,
        request_timeout=0.2,
    ) as (client, wallet, events, _):
        wallet.answers["wc_sessionExtend"] = NOT_ANSWERED
        await _until(lambda: len(wallet.requests("wc_sessionExtend")) >= 2)
        await _until(lambda: "session_expiring" in _names(events))
        expiring = next(e for e in events if e.name == "session_expiring")
        assert 0 < expiring.data["remaining"] <= 3600
        wallet.answers["wc_sessionExtend"] = True
        await _until(lambda: client.sessions[wallet.topic].expiry > time.time() + 6 * 86400)
        # Once extended, the next cycles neither extend nor warn.
        n_extends = len(wallet.requests("wc_sessionExtend"))
        n_events = len(events)
        await asyncio.sleep(0.5)
        assert len(wallet.requests("wc_sessionExtend")) == n_extends
        assert "session_expiring" not in _names(events[n_events:])


async def test_no_auto_extend_by_default_but_expiring_still_emitted():
    wallet_kwargs = {"expiry": int(time.time()) + 3600}
    async with paired(wallet_kwargs=wallet_kwargs, maintenance_interval=0.1) as (
        client,
        wallet,
        events,
        _,
    ):
        await _until(lambda: "session_expiring" in _names(events))
        await asyncio.sleep(0.2)
        assert wallet.requests("wc_sessionExtend") == []


async def test_is_reachable_marks_unreachable_without_deleting():
    async with paired() as (client, wallet, _events, _):
        wallet.answers["wc_sessionPing"] = NOT_ANSWERED
        assert await client.is_reachable(timeout=0.2) is False
        assert client.unreachable == {wallet.topic}
        assert wallet.topic in client.sessions  # not deleted
        with pytest.raises(WalletConnectTimeoutError):
            await client.ping(timeout=0.2)

        # Any message from the wallet clears the mark.
        await wallet.send("wc_sessionEvent", {"event": {"name": "chainChanged", "data": 1}})
        await _until(lambda: not client.unreachable)

        wallet.answers["wc_sessionPing"] = True
        assert await client.is_reachable(timeout=2) is True


async def test_wallet_ping_is_answered():
    async with paired() as (_client, wallet, _events, _):
        res = await wallet.send("wc_sessionPing", {})
        assert res["result"] is True


async def test_relay_disconnect_and_reconnect_events():
    async with paired() as (client, _wallet, events, fake):
        client.relay.reconnect_min_delay = 0.01
        client.relay.reconnect_max_delay = 0.05
        await fake.drop_all()
        await _until(lambda: "relay_reconnect" in _names(events))
        names = _names(events)
        assert names.index("relay_disconnect") < names.index("relay_reconnect")
        relay_event = next(e for e in events if e.name == "relay_reconnect")
        assert relay_event.topic is None
        assert relay_event.session is None


async def test_handler_errors_are_logged_not_raised(caplog):
    async with paired() as (client, wallet, events, _):

        async def boom(event):
            raise RuntimeError("handler bug")

        client.on("session_event", boom)
        await wallet.send("wc_sessionEvent", {"event": {"name": "chainChanged", "data": 1}})
        await _until(lambda: "session_event" in _names(events))
        await asyncio.sleep(0.05)
        assert "session_event handler failed" in caplog.text


def test_sync_client_events_extend_and_reachability():
    with relay_thread() as relay:
        client = SyncSignClient(
            project_id="test", relay_url=relay.url, metadata=META, request_timeout=5
        )
        events: list[ClientEvent] = []
        client.on("*", events.append)
        wallets: list[FakeWallet] = []

        def on_uri(uri: str) -> None:
            wallets.append(relay.submit(FakeWallet(relay.url, uri).start()).result(10))

        session = client.connect(on_uri=on_uri)
        wallet = wallets[0]
        assert client.extend() == WEEK_FROM_NOW
        assert client.is_reachable() is True
        wallet.answers["wc_sessionPing"] = NOT_ANSWERED
        assert client.is_reachable(timeout=0.2) is False
        assert client.unreachable == {session.topic}
        relay.submit(wallet.send("wc_sessionDelete", {"code": 6000, "message": "bye"})).result(5)
        deadline = time.monotonic() + 5
        while "session_delete" not in _names(events) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "session_extend" in _names(events)
        assert "session_delete" in _names(events)
        assert client.sessions == {}
        relay.submit(wallet.close()).result(5)
        client.close()
