"""Crawl / extract / write worker loops -- the real pipeline main.py runs
(proven end-to-end by step 8 Part D's live crawl and crash-resume test).

Every I/O dependency is injected (fetch_fn, score_fn, extract_fn, Writer,
chunk_fn) so these loops are fully testable against stubs -- see
tests/test_crawl_worker.py, tests/test_extract_worker.py.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from storage.canonical import build_canonical_record, detect_license_signal, extract_page_title
from config import MAX_RETRIES
from content.extraction import MalformedExtractionError, parse_qa_json
from crawl.frontier import Frontier, FrontierRow
from crawl.politeness import HostPoliteness
from crawl.robots_cache import RobotsCache
from crawl.scope import normalize_url
from content.sectioning import DEFAULT_SECTION_DEPTH, derive_section
from storage.writer import Writer

DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 5.0
POLL_INTERVAL = 0.05


class FetchTimeout(Exception):
    pass


class FetchHTTPError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class BrowserDriverError(Exception):
    """crawl4ai's shared Playwright browser connection has died -- distinct
    from FetchTimeout (which covers an individual page being slow) because
    every crawl_worker shares ONE browser instance (crawl4ai's
    AsyncPlaywrightCrawlerStrategy holds a single BrowserManager); once its
    driver connection is gone, every worker's next new_page() call fails
    identically, and retrying the same URL against the same dead browser
    is a guaranteed failure, not a transient one. See CLAUDE.md and
    LESSONS_LEARNED.md for the real incident this was found from (an
    unthreaded blocking Chroma call starving the event loop long enough to
    break the driver's connection, now fixed in storage/chunk_store.py --
    this exception and crawl_worker's fail-fast handling of it are the
    second, independent layer: don't keep burning the frontier retrying
    into a dead shared browser even if some other cause kills it again)."""
    pass


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
    politeness: HostPoliteness | None = None,
    poll_interval: float = POLL_INTERVAL,
    fatal_error_sink: list[str] | None = None,
) -> None:
    while True:
        # Checked before claim() too, not just in the row-is-None branch
        # below -- lets a fatal BrowserDriverError (set by any worker,
        # including this one on a prior iteration) stop this worker before
        # it claims and burns another row into a browser that's already
        # known dead, rather than waiting for its next empty-claim poll.
        if frontier.quiescent.is_set():
            return
        row: FrontierRow | None = await frontier.claim()
        if row is None:
            if frontier.quiescent.is_set():
                return
            await asyncio.sleep(poll_interval)
            continue

        policy = None
        if robots_cache is not None:
            policy = await robots_cache.get_policy(row.host)
            if not policy.is_allowed(row.url):
                print(f"[robots] disallowed by robots.txt, skipping: {row.url}")
                await frontier.mark_permanently_failed(row.url, "robots_disallowed")
                continue

        # One shared HostPoliteness instance across every crawl_worker task
        # (see crawl/politeness.py) -- caps concurrent requests to this
        # host and enforces the site's own Crawl-delay if it specified
        # one, else this project's default spacing. nullcontext() when no
        # politeness instance was configured -- same optional-dependency
        # pattern as robots_cache above.
        crawl_delay = policy.crawl_delay if policy is not None else None
        gate = politeness.hold(row.host, crawl_delay) if politeness is not None else nullcontext()

        try:
            async with gate:
                html, raw_hrefs = await fetch_fn(row.url)
        except FetchTimeout as e:
            await frontier.mark_fetch_failed(row.url, f"timeout: {e}")
            continue
        except FetchHTTPError as e:
            await frontier.mark_fetch_failed(row.url, f"http_{e.status_code}")
            continue
        except BrowserDriverError as e:
            # Fail fast, don't retry -- see the class docstring. Every
            # crawl_worker shares one browser; this one is dead, so are
            # the rest. Mark this row terminal (not retryable -- a retry
            # would just be a fourth guaranteed failure against the same
            # dead browser), stop the whole run via the same quiescent
            # signal every worker already polls, and record why so
            # main() can report failure instead of "Done." on exit.
            await frontier.mark_permanently_failed(row.url, f"browser_driver_dead: {e}")
            msg = (
                f"[FATAL] Shared browser driver died ({e}). Stopping the "
                f"run rather than retrying into a dead browser -- "
                f"see LESSONS_LEARNED.md."
            )
            print(msg)
            if fatal_error_sink is not None:
                fatal_error_sink.append(msg)
            frontier.quiescent.set()
            return
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
    chunk_fn: Callable[[str], list[dict]] | None = None,
    extraction_units_fn: Callable[[str], Awaitable[list[str]]] | None = None,
    generation_model: str = "unknown",
    extraction_strategy: str = "first_n_chars",
    section_depth: int = DEFAULT_SECTION_DEPTH,
    seed_prefixes: list[tuple[str, str | None]] | None = None,
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
            units = await extraction_units_fn(content) if extraction_units_fn else [content]
        except Exception as e:
            await frontier.mark_extract_outcome(url, "failed", f"extraction_units: {type(e).__name__}: {e}")
            await frontier.content_done(content_queue)
            continue

        page_title = extract_page_title(content)
        license_signal = detect_license_signal(content)
        section = derive_section(url, depth=section_depth, seed_prefixes=seed_prefixes)
        now = datetime.now(timezone.utc)
        timestamp, crawl_date = now.isoformat(), now.date().isoformat()

        qa_pairs: list[dict] = []
        parsed_any_unit = False
        try:
            for chunk_index, unit in enumerate(units):
                raw_response = await extract_fn(unit)
                try:
                    pairs = parse_qa_json(raw_response)
                except MalformedExtractionError:
                    # One bad unit doesn't fail the whole page -- each unit
                    # is an independent LLM call; a page only fails below
                    # if literally none of its units parsed.
                    continue
                parsed_any_unit = True
                for pair in pairs:
                    qa_pairs.append(build_canonical_record(
                        question=pair["instruction"], answer=pair["response"],
                        source_chunk=unit, chunk_index=chunk_index, source_url=url, section=section,
                        page_title=page_title, generation_model=generation_model,
                        extraction_strategy=extraction_strategy,
                        timestamp=timestamp, crawl_date=crawl_date,
                        license_signal=license_signal,
                    ))
        except RateLimitError as e:
            await frontier.mark_extract_outcome(
                url, "failed", "rate_limited (extraction)",
                backoff_seconds=e.retry_after or DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
            )
            await frontier.content_done(content_queue)
            continue
        except Exception as e:
            await frontier.mark_extract_outcome(url, "failed", f"extraction: {type(e).__name__}: {e}")
            await frontier.content_done(content_queue)
            continue

        if units and not parsed_any_unit:
            await frontier.mark_extract_outcome(
                url, "failed", "malformed_json: no extraction unit produced valid JSON",
            )
            await frontier.content_done(content_queue)
            continue

        chunks = chunk_fn(content) if chunk_fn is not None else None
        await frontier.put_results(results_queue, ExtractionResult(url=url, qa_pairs=qa_pairs, chunks=chunks))
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
