"""Graceful stop for headless contract v1 runs (Brief 3A Step 3, item 8).

First SIGTERM or SIGINT: `GracefulStop.event` is set. headless_crawl then
drains: the frontier stops handing out rows, claimed pages get up to
`stop_grace_seconds` to finish and be written, then the workers are
cancelled, rows still claimed go back to 'queued' (no retry counted), the
crawler context closes -- which closes Chromium -- and the run returns with
`stopped_by_signal`. Exit code 143 (SIGTERM) or 130 (SIGINT).

Second signal: `on_second` runs at once -- headless_crawl's writes a minimal
result.json and calls os._exit. Nothing is cleaned up; see CLAUDE.md for
recovering a browser left behind (by recorded PID only).

Legacy runs never install these handlers.
"""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Callable

SIGNALS = (signal.SIGTERM, signal.SIGINT)
DRAIN_POLL_SECONDS = 0.05


class GracefulStop:
    def __init__(self, on_second: Callable[[str], None] | None = None):
        self.signal_name: str | None = None
        self.count = 0
        self.event = asyncio.Event()
        self._on_second = on_second
        self._installed: list[int] = []

    def handle(self, signame: str) -> None:
        self.count += 1
        if self.count == 1:
            self.signal_name = signame
            self.event.set()
        elif self._on_second is not None:
            self._on_second(signame)

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in SIGNALS:
            loop.add_signal_handler(sig, self.handle, sig.name)
            self._installed.append(sig)

    def uninstall(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in self._installed:
            loop.remove_signal_handler(sig)
        self._installed.clear()


async def drain(frontier, grace_seconds: float, clock: Callable[[], float] = time.monotonic) -> dict:
    """Stop new claims and wait until nothing is in flight, or the grace
    period ends. Returns {"drained": bool, "waited_seconds": float}."""
    frontier.begin_drain()
    started = clock()
    while frontier.in_flight > 0 and clock() - started < grace_seconds:
        await asyncio.sleep(DRAIN_POLL_SECONDS)
    return {"drained": frontier.in_flight == 0, "waited_seconds": round(clock() - started, 3)}
