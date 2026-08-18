"""Crawl / extract / write worker loops. Built alongside orchestrator.py,
not replacing it in place -- orchestrator.py stays importable as a working
fallback until this path is proven (see the rebuild plan). Nothing here is
wired into main.py yet.

Every I/O dependency is injected (fetch_fn, score_fn, extract_fn, Writer)
so these loops are fully testable against stubs -- see
tests/test_crawl_worker.py, tests/test_extract_worker.py.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from config import MAX_RETRIES
from extraction import MalformedExtractionError, parse_qa_json
from frontier import Frontier, FrontierRow
from robots_cache import RobotsCache
from scope import normalize_url
from writer import Writer

DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 5.0
POLL_INTERVAL = 0.05


class FetchTimeout(Exception):
    pass


class FetchHTTPError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class RateLimitError(Exception):
    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(f"rate limited, retry_after={retry_after}")


@dataclass
class ExtractionResult:
    url: str
    qa_pairs: list[dict]
    chunks: list[dict] | None = None


FetchFn = Callable[[str], Awaitable[tuple[str, list[str]]]]  # url -> (html, raw_hrefs)
ScoreFn = Callable[[str, str], Awaitable[float]]  # (url, content) -> relevance score
ExtractFn = Callable[[str], Awaitable[str]]  # content -> raw LLM response text
ScopeCheck = Callable[[str], bool]


async def crawl_worker(
    frontier: Frontier,
    fetch_fn: FetchFn,
    content_queue: asyncio.Queue,
    scope_check: ScopeCheck,
    robots_cache: RobotsCache | None = None,
    poll_interval: float = POLL_INTERVAL,
) -> None:
    while True:
        row: FrontierRow | None = await frontier.claim()
        if row is None:
            if frontier.quiescent.is_set():
                return
            await asyncio.sleep(poll_interval)
            continue

        if robots_cache is not None:
            policy = await robots_cache.get_policy(row.host)
            if not policy.is_allowed(row.url):
                print(f"[robots] disallowed by robots.txt, skipping: {row.url}")
                await frontier.mark_permanently_failed(row.url, "robots_disallowed")
                continue

        try:
            html, raw_hrefs = await fetch_fn(row.url)
        except FetchTimeout as e:
            await frontier.mark_fetch_failed(row.url, f"timeout: {e}")
            continue
        except FetchHTTPError as e:
            await frontier.mark_fetch_failed(row.url, f"http_{e.status_code}")
            continue
        except Exception as e:
            await frontier.mark_fetch_failed(row.url, f"{type(e).__name__}: {e}")
            continue

        children = []
        for href in raw_hrefs:
            normalized = normalize_url(href, row.url)
            if normalized is None:
                continue
            if not scope_check(normalized):
                continue
            children.append((normalized, row.section_label))

        await frontier.insert_children(row.url, row.depth, children)
        await frontier.put_content(content_queue, (row.url, html))


async def extract_worker(
    frontier: Frontier,
    content_queue: asyncio.Queue,
    results_queue: asyncio.Queue,
    score_fn: ScoreFn,
    extract_fn: ExtractFn,
    extract_threshold: float,
    follow_threshold: float,
    poll_interval: float = POLL_INTERVAL,
) -> None:
    while True:
        try:
            url, content = await asyncio.wait_for(content_queue.get(), timeout=poll_interval)
        except asyncio.TimeoutError:
            if frontier.quiescent.is_set():
                return
            continue

        try:
            score = await score_fn(url, content)
        except RateLimitError as e:
            await frontier.mark_extract_outcome(
                url, "failed", "rate_limited (scoring)",
                backoff_seconds=e.retry_after or DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
            )
            await frontier.content_done(content_queue)
            continue
        except Exception as e:
            await frontier.mark_extract_outcome(url, "failed", f"scoring: {type(e).__name__}: {e}")
            await frontier.content_done(content_queue)
            continue

        promote = score >= follow_threshold
        await frontier.score_and_resolve_children(url, score, promote)

        if score < extract_threshold:
            await frontier.mark_extract_outcome(url, "skipped_extract")
            await frontier.content_done(content_queue)
            continue

        try:
            raw_response = await extract_fn(content)
            qa_pairs = parse_qa_json(raw_response)
        except RateLimitError as e:
            await frontier.mark_extract_outcome(
                url, "failed", "rate_limited (extraction)",
                backoff_seconds=e.retry_after or DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
            )
            await frontier.content_done(content_queue)
            continue
        except MalformedExtractionError as e:
            await frontier.mark_extract_outcome(url, "failed", f"malformed_json: {e}")
            await frontier.content_done(content_queue)
            continue
        except Exception as e:
            await frontier.mark_extract_outcome(url, "failed", f"extraction: {type(e).__name__}: {e}")
            await frontier.content_done(content_queue)
            continue

        await frontier.put_results(results_queue, ExtractionResult(url=url, qa_pairs=qa_pairs))
        await frontier.content_done(content_queue)


async def writer_worker(
    frontier: Frontier,
    results_queue: asyncio.Queue,
    writer: Writer,
    poll_interval: float = POLL_INTERVAL,
) -> None:
    while True:
        try:
            result: ExtractionResult = await asyncio.wait_for(
                results_queue.get(), timeout=poll_interval
            )
        except asyncio.TimeoutError:
            if frontier.quiescent.is_set():
                return
            continue

        try:
            await writer.write(result.url, result.qa_pairs, result.chunks)
        except Exception as e:
            await frontier.mark_write_failed(result.url, f"{type(e).__name__}: {e}")
        else:
            await frontier.mark_written(result.url)
        await frontier.results_done(results_queue)
