"""Sole owner of the JSONL file handle and (later, step 7) the Chroma
client. Nothing else in the pipeline should hold a reference to either --
enforced two ways: structurally, crawl_worker/extract_worker's signatures
in pipeline.py never receive a Writer instance or its internals, only the
writer() loop does; and at runtime, this class refuses concurrent calls
outright (see _guard below) rather than silently serializing them, so a
violation of "one caller only" surfaces immediately instead of masquerading
as correct behavior.

This replaces the old asyncio.Lock-per-write pattern from output_manager.py
entirely -- that lock existed because multiple workers wrote directly. With
exactly one caller, no internal locking is needed for correctness, only
this guard to catch it if that assumption is ever violated.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable

import aiofiles

ChromaUpsertFn = Callable[[str, list[dict]], Awaitable[None]]  # (url, chunks) -- each chunk needs its source url


class Writer:
    def __init__(self, jsonl_path: str, chroma_upsert_fn: ChromaUpsertFn | None = None):
        self._jsonl_path = jsonl_path
        self._chroma_upsert_fn = chroma_upsert_fn
        self._written_urls: set[str] = set()
        self._guard = asyncio.Lock()
        self._preload_written_urls()

    def _preload_written_urls(self) -> None:
        """Same pattern as output_manager.py's existing instruction-text
        preload, keyed by source_url instead -- this is what makes a
        crash-resumed run's redundant extraction not produce a duplicate
        JSONL row (see LESSONS_LEARNED.md #13 for why this lives here
        rather than a same-transaction SQLite marker)."""
        path = Path(self._jsonl_path)
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                source_url = data.get("source_url")
                if source_url:
                    self._written_urls.add(source_url)

    def already_written(self, url: str) -> bool:
        return url in self._written_urls

    async def write(self, url: str, qa_pairs: list[dict], chunks: list[dict] | None = None) -> None:
        if self._guard.locked():
            raise RuntimeError(
                "Writer.write() called while another call is still in flight -- "
                "Writer must have exactly one caller (the writer worker loop in "
                "pipeline.py). This means something outside that loop obtained a "
                "reference to this Writer instance, which should be structurally "
                "impossible; fix the caller, don't relax this guard."
            )
        async with self._guard:
            if not self.already_written(url) and qa_pairs:
                lines = [
                    json.dumps({**pair, "source_url": url}) + "\n" for pair in qa_pairs
                ]
                async with aiofiles.open(self._jsonl_path, mode="a", encoding="utf-8") as f:
                    await f.writelines(lines)
                self._written_urls.add(url)
            if chunks and self._chroma_upsert_fn is not None:
                await self._chroma_upsert_fn(url, chunks)
