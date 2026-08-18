"""Per-host politeness: bounds concurrent in-flight requests to one host
and enforces a minimum spacing between successive requests to it --
ROADMAP.md #9's remaining gap after Allow/Disallow precedence was fixed
in step 8 Part D. N crawl_worker tasks all claim from the same shared
frontier and can land on the same host simultaneously, with nothing
throttling requests to that host specifically; a capped 20-page run
against one host never surfaced this, a few-hundred-page run against a
single host would.

One HostPoliteness instance must be shared across every crawl_worker
task (main.py creates one, passes the same object to all of them) --
per-host state has to be shared for the semaphore and delay to mean
anything across workers, not reset per worker.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from config import DEFAULT_POLITENESS_DELAY_SECONDS, MAX_CONCURRENT_REQUESTS_PER_HOST


class HostPoliteness:
    def __init__(
        self,
        max_concurrent_per_host: int = MAX_CONCURRENT_REQUESTS_PER_HOST,
        default_delay_seconds: float = DEFAULT_POLITENESS_DELAY_SECONDS,
    ):
        self._max_concurrent = max_concurrent_per_host
        self._default_delay = default_delay_seconds
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request_at: dict[str, float] = {}

    def _semaphore(self, host: str) -> asyncio.Semaphore:
        if host not in self._semaphores:
            self._semaphores[host] = asyncio.Semaphore(self._max_concurrent)
        return self._semaphores[host]

    def _lock(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    @asynccontextmanager
    async def hold(self, host: str, crawl_delay: float | None = None):
        """Acquire this host's concurrency slot, wait out any remaining
        politeness delay since the last request *to this host* started,
        then yield -- release the slot on the way out regardless of how
        the caller's request went. `crawl_delay` overrides this
        instance's default when robots.txt specifies one for the host
        (a site's own stated preference takes precedence over this
        project's default).

        Spacing is measured request-start to request-start, not
        end-to-start -- a request that itself takes longer than the
        delay already provides real spacing without an extra sleep; this
        is the simpler of two defensible interpretations of Crawl-delay
        and not the only one real crawlers use.

        The per-host lock is held only across a timestamp read/sleep/
        write, never across the caller's actual request -- so it
        serializes request *starts* for one host (the intended
        throttling effect) without blocking workers assigned to other
        hosts, and without holding the semaphore's concurrency slot idle
        any longer than the delay itself requires.
        """
        sem = self._semaphore(host)
        await sem.acquire()
        try:
            delay = self._default_delay if crawl_delay is None else crawl_delay
            async with self._lock(host):
                last = self._last_request_at.get(host)
                if last is not None:
                    wait = delay - (time.monotonic() - last)
                    if wait > 0:
                        await asyncio.sleep(wait)
                self._last_request_at[host] = time.monotonic()
            yield
        finally:
            sem.release()
