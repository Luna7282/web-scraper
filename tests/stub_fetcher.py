"""Content-agnostic fetch stub: no network, no crawl4ai. Register content
per URL (real cached HTML for step 8's kill test, or a small synthetic
marker for a frontier-mechanics test like this step's quiescence test) with
a configurable delay, then `await fetch(url)` it back.

The same object serves both use cases -- what's registered is what's
returned, real HTML fixture or synthetic string, the stub doesn't care.
"""
from __future__ import annotations

import asyncio
from pathlib import Path


class StubFetcher:
    def __init__(self, default_delay: float = 0.0):
        self._content: dict[str, str] = {}
        self._delays: dict[str, float] = {}
        self._default_delay = default_delay
        self.fetch_log: list[str] = []

    def register(self, url: str, content: str, delay: float | None = None) -> None:
        self._content[url] = content
        if delay is not None:
            self._delays[url] = delay

    def register_fixture(self, url: str, html_path: str, delay: float | None = None) -> None:
        self.register(url, Path(html_path).read_text(encoding="utf-8"), delay)

    async def fetch(self, url: str) -> str:
        self.fetch_log.append(url)
        await asyncio.sleep(self._delays.get(url, self._default_delay))
        if url not in self._content:
            raise KeyError(f"StubFetcher: no content registered for {url!r}")
        return self._content[url]
