"""Run the fake relay (and scripted peers) on a background event loop.

Needed by tests that drive a synchronous client (FastAPI ``TestClient``, MCP stdio, ...)
which would otherwise block the loop serving the fake relay.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any

from tests.fake_relay import FakeRelay, run_fake_relay


class RelayThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.fake: FakeRelay | None = None
        self.url = ""
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, name="fake-relay", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        async with run_fake_relay() as (fake, url):
            self.fake, self.url = fake, url
            self._ready.set()
            await self._stop.wait()

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("fake relay did not start")

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)


@contextmanager
def relay_thread() -> Iterator[RelayThread]:
    rt = RelayThread()
    rt.start()
    try:
        yield rt
    finally:
        rt.stop()
