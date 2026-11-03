"""Relay auto-reconnect: backoff, resubscribe, missed-message fetch, in-flight policy."""

from __future__ import annotations

import asyncio

import pytest

from tests.fake_relay import run_fake_relay
from tests.test_sign_client import scripted_wallet
from walletconnect.exceptions import RelayDisconnectedError
from walletconnect.relay import RelayClient, RelayMessage
from walletconnect.sign import SignClient
from walletconnect.types import Metadata

TOPIC = "ab" * 32
FAST = {"reconnect_min_delay": 0.02, "reconnect_max_delay": 0.1}


def _client(url: str, **kwargs) -> RelayClient:
    return RelayClient(project_id="p", relay_url=url, **{**FAST, **kwargs})


async def _until(predicate, timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


def test_backoff_is_capped_and_jittered():
    client = RelayClient(project_id="p", reconnect_min_delay=0.5, reconnect_max_delay=30)
    for attempt in range(40):
        cap = min(30.0, 0.5 * 2**attempt)
        delays = {client._backoff(attempt) for _ in range(20)}
        assert all(cap / 2 <= d <= cap for d in delays)
        assert len(delays) > 1  # jitter


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1006, 1001, 4010, 1011])
async def test_reconnects_resubscribes_and_fetches_missed(code):
    async with run_fake_relay() as (fake, url):
        received: asyncio.Queue[RelayMessage] = asyncio.Queue()
        events: list[str] = []
        async with _client(url) as client:
            client.on_message(received.put_nowait)
            client.on_connection_event(events.append)
            old_auth = client.auth_key
            await client.subscribe(TOPIC)

            fake.reject_status = 503  # keep it down while the peer publishes
            await fake.drop_all(code)
            await _until(lambda: not client.connected)
            # A peer publishes while we are offline: it lands in the relay mailbox.
            await fake._publish({"topic": TOPIC, "message": "while-offline", "tag": 1})
            fake.reject_status = None

            await _until(lambda: client.reconnect_count == 1)
            msg = await asyncio.wait_for(received.get(), timeout=5)
            assert msg.message == "while-offline"
            assert events == ["disconnect", "reconnect"]
            assert client.auth_key is old_auth  # same did:key, fresh JWT
            # The relay knows our subscription again: live messages flow.
            async with RelayClient(project_id="p", relay_url=url) as peer:
                await peer.publish(TOPIC, "live", ttl=300, tag=1)
            seen = set()
            while "live" not in seen:
                seen.add((await asyncio.wait_for(received.get(), timeout=5)).message)


@pytest.mark.asyncio
async def test_request_waits_for_reconnect():
    async with run_fake_relay() as (fake, url), _client(url, request_timeout=5) as c:
        fake.reject_status = 503
        await fake.drop_all()
        await _until(lambda: not c.connected)
        publish = asyncio.create_task(c.publish(TOPIC, "queued", ttl=300, tag=1))
        await asyncio.sleep(0.3)
        assert not publish.done()
        fake.reject_status = None
        assert await asyncio.wait_for(publish, 5) is True
        assert [m["message"] for m in fake.mailbox[TOPIC]] == ["queued"]


@pytest.mark.asyncio
async def test_in_flight_publish_is_retried_after_drop():
    async with run_fake_relay() as (fake, url), _client(url) as c:
        fake.drop_after = "irn_publish"  # stored, but the ack never arrives
        assert await c.publish(TOPIC, "once", ttl=300, tag=1) is True
        assert c.reconnect_count == 1
        # Re-sent idempotently: same message, consumers dedupe by content.
        assert {m["message"] for m in fake.mailbox[TOPIC]} == {"once"}


@pytest.mark.asyncio
async def test_outage_longer_than_timeout_raises_specific_error():
    async with run_fake_relay() as (fake, url), _client(url, request_timeout=0.5) as c:
        fake.reject_status = 503
        await fake.drop_all()
        await _until(lambda: not c.connected)
        with pytest.raises(RelayDisconnectedError, match="not restored"):
            await c.publish(TOPIC, "x", ttl=300, tag=1)
        assert c.reconnecting  # still trying in the background
        fake.reject_status = None
        await _until(lambda: c.connected)
        assert await c.publish(TOPIC, "x", ttl=300, tag=1) is True


@pytest.mark.asyncio
async def test_auth_rejection_stops_reconnecting():
    async with run_fake_relay() as (fake, url), _client(url) as c:
        fake.reject_status = 401
        await fake.drop_all()
        await _until(lambda: not c.reconnecting and not c.connected)
        with pytest.raises(RelayDisconnectedError, match="gave up"):
            await c.publish(TOPIC, "x", ttl=300, tag=1)
        # An explicit connect() is allowed to try again.
        fake.reject_status = None
        await c.connect()
        assert await c.publish(TOPIC, "x", ttl=300, tag=1) is True


@pytest.mark.asyncio
async def test_max_attempts():
    async with (
        run_fake_relay() as (fake, url),
        RelayClient(project_id="p", relay_url=url, reconnect_max_attempts=3, **FAST) as c,
    ):
        fake.reject_status = 503
        before = fake.connection_count
        await fake.drop_all()
        await _until(lambda: c._fatal is not None)
        assert fake.connection_count == before  # every upgrade was refused
        with pytest.raises(RelayDisconnectedError, match="gave up"):
            await c.publish(TOPIC, "x", ttl=300, tag=1)


@pytest.mark.asyncio
async def test_auto_reconnect_disabled_fails_fast():
    async with run_fake_relay() as (fake, url), _client(url, auto_reconnect=False) as c:
        await fake.drop_all()
        await _until(lambda: not c.connected)
        await asyncio.sleep(0.1)
        assert fake.clients == []
        with pytest.raises(RelayDisconnectedError):
            await c.publish(TOPIC, "x", ttl=300, tag=1)


@pytest.mark.asyncio
async def test_close_stops_reconnect_loop():
    async with run_fake_relay() as (fake, url):
        c = RelayClient(project_id="p", relay_url=url, **FAST)
        await c.connect()
        fake.reject_status = 503
        await fake.drop_all()
        await _until(lambda: c.reconnecting)
        await c.close()
        assert not c.reconnecting
        fake.reject_status = None
        await asyncio.sleep(0.2)
        assert fake.clients == []
        with pytest.raises(RelayDisconnectedError, match="closed"):
            await c.publish(TOPIC, "x", ttl=300, tag=1)


@pytest.mark.asyncio
async def test_sign_client_survives_relay_drop():
    async with run_fake_relay() as (fake, url):
        client = SignClient(
            project_id="test",
            relay_url=url,
            metadata=Metadata(name="reconnect-test", url="https://example.com"),
            connect_timeout=10,
            request_timeout=10,
        )
        client.relay.reconnect_min_delay = 0.02
        uri, task = await client.connect(chains=(1,))
        wallet = asyncio.create_task(scripted_wallet(url, uri, n_requests=3))
        session = await client.wait_for_session(task)
        address = session.eip155_accounts[0].split(":")[-1]
        assert await client.personal_sign("before", address) == "0xdeadbeef"

        await fake.drop_all()  # both dApp and wallet lose the socket
        await _until(lambda: client.relay.reconnect_count == 1)
        assert await client.personal_sign("after", address) == "0xdeadbeef"

        # A request issued while the relay is down waits for it and still completes.
        fake.reject_status = 503
        await fake.drop_all()
        await _until(lambda: not client.relay.connected)
        pending = asyncio.create_task(client.personal_sign("during", address))
        await asyncio.sleep(0.2)
        fake.reject_status = None
        assert await asyncio.wait_for(pending, 10) == "0xdeadbeef"
        await wallet
        await client.close()
