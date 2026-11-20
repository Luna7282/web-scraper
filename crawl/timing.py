"""Opt-in timing log for the crawl/extract pipeline (config.EXTRACT_TIMING_LOG).

Off by default: when disabled, main.py never builds a TimingLog and passes
`timing=None` to the workers, which then skip every timing branch -- the
pre-timing code path, unchanged.

Event-loop invariant (CLAUDE.md): `record()` is synchronous and never
blocks -- it only does `put_nowait` onto an unbounded asyncio.Queue, so
instrumenting a worker adds no new await points to it. One drain task
(`start()`) is the only thing that touches the file, and it writes through
`asyncio.to_thread`, so the blocking file append never runs on the shared
loop. A single drain task is also what makes `to_thread` sufficient here
without a lock: the file has exactly one writer at a time.

Lines are JSON objects with at least `event` and `ts` (wall-clock epoch
seconds); durations are `time.perf_counter()` deltas in seconds. Event
kinds written by crawl/pipeline.py and main.py:
  run_start / run_end   -- run config, total wall seconds
  fetch                 -- url, seconds, outcome (ok / exception class name)
  score                 -- url, seconds, queue_wait_seconds
  extract_call          -- url, chunk_index, chars_in, seconds, provider,
                           model, outcome (ok / malformed / rate_limited / error),
                           usage (token counts from the response, or null)
  quality_call          -- url, chunk_index, seconds, outcome
  page                  -- url, outcome, units, pairs, extraction_seconds
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextvars import ContextVar

_STOP = object()

# Token usage of the most recent extraction LLM call, published by
# main.py::_make_extract_fn (only when built with record_usage=True, i.e.
# when timing is on) and read back by extract_worker right after it awaits
# extract_fn. A ContextVar rather than a return value so extract_fn's
# `content -> str` contract is unchanged: a value set inside an awaited
# coroutine is visible to its caller in the same task, and each task gets
# its own copy, so concurrent calls in separate tasks never see each
# other's usage.
call_usage: ContextVar[dict | None] = ContextVar("call_usage", default=None)


def usage_from_message(message) -> dict | None:
    """Token counts from a LangChain chat response, or None if the provider
    reported none. reasoning_tokens is the provider's thinking-token count
    when it reports one (OpenAI-compatible `completion_tokens_details.
    reasoning_tokens`, surfaced by LangChain as output_token_details.reasoning)."""
    meta = getattr(message, "usage_metadata", None)
    if not meta:
        return None
    details = meta.get("output_token_details") or {}
    return {
        "input_tokens": meta.get("input_tokens"),
        "output_tokens": meta.get("output_tokens"),
        "total_tokens": meta.get("total_tokens"),
        "reasoning_tokens": details.get("reasoning"),
    }


def _append_lines(path: str, events: list[dict]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(event, default=str) + "\n" for event in events)


class TimingLog:
    def __init__(self, path: str, provider: str | None = None, model: str | None = None):
        self.path = path
        self.provider = provider
        self.model = model
        # Unbounded on purpose: record() must never await or block a worker.
        # Timing events are small and few (a handful per page), so growth is
        # bounded by the run itself.
        self._queue: asyncio.Queue = asyncio.Queue()
        self._enqueued_at: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._started: float | None = None
        self._warned = False

    def record(self, event: str, **fields) -> None:
        self._queue.put_nowait({"event": event, "ts": round(time.time(), 3), **fields})

    def llm_fields(self) -> dict:
        return {"provider": self.provider, "model": self.model}

    def mark_enqueued(self, url: str) -> None:
        """Called by crawl_worker when a fetched page is handed to
        content_queue; paired with pop_queue_wait() in extract_worker. The
        resulting wait spans fetch-complete -> extract-worker pickup, so it
        includes any backpressure wait to get into a full queue."""
        self._enqueued_at[url] = time.perf_counter()

    def pop_queue_wait(self, url: str) -> float | None:
        started = self._enqueued_at.pop(url, None)
        return None if started is None else round(time.perf_counter() - started, 4)

    def start(self) -> None:
        self._started = time.perf_counter()
        self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            stop = any(item is _STOP for item in batch)
            events = [item for item in batch if item is not _STOP]
            if events:
                try:
                    await asyncio.to_thread(_append_lines, self.path, events)
                except Exception as e:  # noqa: BLE001 -- timing must never break a crawl
                    # Diagnostics only -- a timing-file problem must never
                    # take down the crawl. Warn once, drop the lines.
                    if not self._warned:
                        print(f"[timing] could not write {self.path}: {type(e).__name__}: {e}")
                        self._warned = True
            if stop:
                return

    async def close(self) -> None:
        """Flush everything recorded so far and stop the drain task. Uses a
        sentinel rather than cancelling, so a write already in flight in its
        worker thread finishes before the final batch is appended -- never
        two concurrent appends to the same file. Records a final run_end
        event (wall seconds since start()) first."""
        if self._task is None:
            return
        self.record("run_end", wall_seconds=round(time.perf_counter() - self._started, 3))
        self._queue.put_nowait(_STOP)
        await self._task
        self._task = None
