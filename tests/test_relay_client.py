import asyncio
import os

import pytest

from tests.fake_relay import run_fake_relay
from walletconnect.exceptions import MissingProjectIdError, RelayConnectionError
from walletconnect.relay import RelayClient, RelayMessage
from walletconnect.relay.rpc import make_request


def test_requires_project_id(monkeypatch):
    monkeypatch.delenv("WALLETCONNECT_PROJECT_ID", raising=False)
    with pytest.raises(MissingProjectIdError):
        RelayClient()


def test_make_request_id_shape():
    req = make_request("irn_publish", {"topic": "ab"})
    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "irn_publish"
    assert isinstance(req["id"], int)
    assert abs(req["id"]) <= 2**53 - 1  # JS-safe (matches @walletconnect/jsonrpc-utils)


@pytest.mark.asyncio
async def test_publish_subscribe_round_trip():
    async with run_fake_relay() as (_fake, base_url):
        received: asyncio.Queue[RelayMessage] = asyncio.Queue()

        async with RelayClient(project_id="test-project", relay_url=base_url) as client:
            client.on_message(received.put_nowait)
            topic = "aa" * 32
            sub_id = await client.subscribe(topic)
            assert sub_id.startswith("sub-")

            ok = await client.publish(topic, "hello-envelope", ttl=300, tag=1100)
            assert ok is True

            msg = await asyncio.wait_for(received.get(), timeout=2)
            assert msg.topic == topic
            assert msg.message == "hello-envelope"
            assert msg.tag == 1100
            assert msg.subscription_id == sub_id

            await client.unsubscribe(topic)
            assert topic not in client._subscriptions


@pytest.mark.asyncio
async def test_fetch_messages_from_mailbox():
    async with run_fake_relay() as (fake, base_url):
        topic = "bb" * 32
        # Publish with no subscribers → lands in mailbox.
        async with RelayClient(project_id="test-project", relay_url=base_url) as publisher:
            await publisher.publish(topic, "cached", ttl=300, tag=1000)

        async with RelayClient(project_id="test-project", relay_url=base_url) as client:
            messages, has_more = await client.fetch_messages(topic)
            assert has_more is False
            assert len(messages) == 1
            assert messages[0].message == "cached"
            assert messages[0].tag == 1000
            assert fake.mailbox[topic]


@pytest.mark.asyncio
async def test_env_project_id(monkeypatch):
    monkeypatch.setenv("WALLETCONNECT_PROJECT_ID", "from-env")
    async with run_fake_relay() as (_fake, base_url), RelayClient(relay_url=base_url) as client:
        assert client.project_id == "from-env"
        assert client.client_id.startswith("did:key:z")


@pytest.mark.asyncio
async def test_request_without_connect_fails():
    client = RelayClient(project_id="x", relay_url="ws://127.0.0.1:1")
    with pytest.raises(RelayConnectionError, match="Not connected"):
        await client.publish("aa" * 32, "x", ttl=1, tag=1)


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_relay_connect():
    if not os.environ.get("WALLETCONNECT_PROJECT_ID"):
        pytest.skip("WALLETCONNECT_PROJECT_ID not set")
    client = RelayClient()
    await client.connect()
    assert client.connected
    assert client.client_id.startswith("did:key:z")
    topic = "cc" * 32
    sub_id = await client.subscribe(topic)
    assert isinstance(sub_id, str)
    assert sub_id
    await client.unsubscribe(topic)
    await client.close()
