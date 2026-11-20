"""Crawl / extract / write worker loops -- the real pipeline main.py runs
(proven end-to-end by step 8 Part D's live crawl and crash-resume test).

Every I/O dependency is injected (fetch_fn, score_fn, extract_fn, Writer,
chunk_fn) so these loops are fully testable against stubs -- see
tests/test_crawl_worker.py, tests/test_extract_worker.py.
"""
from __future__ import annotations

import asyncio
import functools
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from crawl.timing import TimingLog
    from decisions import DecisionBackend

from storage.canonical import build_canonical_record, detect_license_signal, extract_page_title
from config import MAX_RETRIES, get_threshold
from content.extraction import MalformedExtractionError, parse_qa_json
from crawl.frontier import Frontier, FrontierRow
from crawl.politeness import HostPoliteness
from crawl.robots_cache import RobotsCache
from crawl.scope import normalize_url
from crawl.timing import call_usage
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


async def _run_in_order(factories: list[Callable[[], Awaitable]]) -> list:
    """Run every factory's coroutine concurrently in one TaskGroup and
    return their results in factory order (not completion order). Any
    failure cancels the rest (TaskGroup semantics) and is re-raised as the
    plain exception -- not an ExceptionGroup -- so callers keep their
    ordinary `except RateLimitError` / `except Exception` handling. If
    several calls failed before cancellation landed, a RateLimitError wins:
    it's the one failure with its own retry-later path (release the row with
    backoff), and reporting a sibling's error instead would skip it.
    Cancelling the caller cancels every child; nothing is left running."""
    results: list = [None] * len(factories)

    async def run(index: int, factory: Callable[[], Awaitable]) -> None:
        results[index] = await factory()

    try:
        async with asyncio.TaskGroup() as tg:
            for index, factory in enumerate(factories):
                tg.create_task(run(index, factory))
    except ExceptionGroup as group:
        leaves = _leaf_exceptions(group)
        raise next((e for e in leaves if isinstance(e, RateLimitError)), leaves[0]) from None
    return results


def _leaf_exceptions(group: BaseExceptionGroup) -> list[BaseException]:
    leaves: list[BaseException] = []
    for e in group.exceptions:
        leaves.extend(_leaf_exceptions(e) if isinstance(e, BaseExceptionGroup) else [e])
    return leaves


# Timing helpers (crawl/timing.py, config.EXTRACT_TIMING_LOG). Every one is
# a no-op when timing is None -- the default -- and none of them awaits.
def _since(started: float) -> float:
    return round(time.perf_counter() - started, 4)


def _record_extract_call(
    timing: TimingLog | None, url: str, chunk_index: int, unit: str, started: float,
    outcome: str, seconds: float | None = None,
) -> None:
    if timing is None:
        return
    timing.record(
        "extract_call", url=url, chunk_index=chunk_index, chars_in=len(unit),
        seconds=seconds if seconds is not None else _since(started),
        outcome=outcome, usage=call_usage.get(), **timing.llm_fields(),
    )


def _record_page(
    timing: TimingLog | None, url: str, outcome: str, units: int, pairs: int, started: float | None,
) -> None:
    if timing is None:
        return
    timing.record(
        "page", url=url, outcome=outcome, units=units, pairs=pairs,
        extraction_seconds=_since(started) if started is not None else 0.0,
    )


async def crawl_worker(
    frontier: Frontier,
    fetch_fn: FetchFn,
    content_queue: asyncio.Queue,
    scope_check: ScopeCheck,
    robots_cache: RobotsCache | None = None,
    politeness: HostPoliteness | None = None,
    poll_interval: float = POLL_INTERVAL,
    fatal_error_sink: list[str] | None = None,
    timing: TimingLog | None = None,
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
                if timing is None:
                    html, raw_hrefs = await fetch_fn(row.url)
                else:
                    # Timed inside the politeness gate: fetch time only,
                    # not time spent waiting for the per-host slot.
                    fetch_started = time.perf_counter()
                    try:
                        html, raw_hrefs = await fetch_fn(row.url)
                    except Exception as e:
                        timing.record("fetch", url=row.url, seconds=_since(fetch_started),
                                      outcome=type(e).__name__)
                        raise
                    timing.record("fetch", url=row.url, seconds=_since(fetch_started), outcome="ok")
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
        if timing is not None:
            timing.mark_enqueued(row.url)
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
    quality_backend: DecisionBackend | None = None,
    quality_threshold: float | None = None,
    timing: TimingLog | None = None,
    unit_concurrency: int = 1,
) -> None:
    """unit_concurrency (config.EXTRACT_UNIT_CONCURRENCY, default 1): how
    many of one page's extraction units -- and, with a quality_backend, its
    pair-scoring calls -- may be in flight at once. 1 is the original
    strictly-sequential behaviour; see the comment at the extraction loop."""
    if unit_concurrency < 1:
        raise ValueError(f"unit_concurrency must be >= 1, got {unit_concurrency}")

    def make_unit_processor(url, section, page_title, timestamp, crawl_date, license_signal):
        """Per-page closures over this page's fields (built once per page,
        before the loop variables move on)."""
        async def score_pair(chunk_index: int, pair: dict, sem: asyncio.Semaphore | None):
            """One quality-scoring call. Not caught here -- a DecisionError/
            RateLimitError falls through to the same except blocks below that
            handle extract_fn's failures (fail loudly, mark_extract_outcome(...,
            'failed', reason), never a silent fallback to keeping the pair)."""
            async with sem if sem is not None else nullcontext():
                quality_started = time.perf_counter()
                try:
                    answer = await quality_backend.noul(
                        f"Instruction: {pair['instruction']}\nResponse: {pair['response']}",
                        "Is this a good instruction-response training pair?",
                    )
                except Exception as e:
                    if timing is not None:
                        timing.record(
                            "quality_call", url=url, chunk_index=chunk_index,
                            seconds=_since(quality_started),
                            outcome="rate_limited" if isinstance(e, RateLimitError) else "error",
                        )
                    raise
                if timing is not None:
                    timing.record("quality_call", url=url, chunk_index=chunk_index,
                                  seconds=_since(quality_started), outcome="ok")
            return answer.value, answer.method

        async def process_unit(chunk_index: int, unit: str, sem: asyncio.Semaphore | None) -> list[dict] | None:
            """One extraction unit: one extract_fn call, parse, optional
            quality scoring. Returns the unit's canonical records, or None if
            its response was malformed -- one bad unit doesn't fail the whole
            page (each unit is an independent LLM call; a page only fails
            below if literally none of its units parsed). Any other exception
            propagates. With a semaphore, a permit is held only around each
            LLM call -- never while waiting on this unit's own quality calls,
            which need permits too (holding one there could deadlock)."""
            if timing is not None:
                call_usage.set(None)  # never attribute a previous call's usage to this one
            async with sem if sem is not None else nullcontext():
                call_started = time.perf_counter()
                try:
                    raw_response = await extract_fn(unit)
                except Exception as e:
                    _record_extract_call(timing, url, chunk_index, unit, call_started,
                                         "rate_limited" if isinstance(e, RateLimitError) else "error")
                    raise
                call_seconds = _since(call_started)
            try:
                pairs = parse_qa_json(raw_response)
            except MalformedExtractionError:
                _record_extract_call(timing, url, chunk_index, unit, call_started, "malformed",
                                     seconds=call_seconds)
                return None
            _record_extract_call(timing, url, chunk_index, unit, call_started, "ok", seconds=call_seconds)

            scores = [(None, None)] * len(pairs)
            if quality_backend is not None:
                if sem is None:
                    scores = [await score_pair(chunk_index, pair, None) for pair in pairs]
                else:
                    scores = await _run_in_order(
                        [functools.partial(score_pair, chunk_index, pair, sem) for pair in pairs]
                    )
            records = []
            for pair, (quality_score, quality_method) in zip(pairs, scores):
                if quality_backend is not None:
                    # None (the default): per-method threshold, looked up by
                    # the answering backend's own method -- Jev/Laya/
                    # traditional score quality on different distributions
                    # (see config.DECISION_THRESHOLDS), so reusing one fixed
                    # number regardless of which backend answered would be
                    # the same category error dedup's shared 0.4 threshold
                    # turned out to be. An explicit quality_threshold still
                    # overrides this for every method, same as before.
                    active_threshold = (
                        quality_threshold if quality_threshold is not None
                        else get_threshold("sample_quality", quality_method)
                    )
                    if quality_score < active_threshold:
                        continue
                records.append(build_canonical_record(
                    question=pair["instruction"], answer=pair["response"],
                    source_chunk=unit, chunk_index=chunk_index, source_url=url, section=section,
                    page_title=page_title, generation_model=generation_model,
                    extraction_strategy=extraction_strategy,
                    timestamp=timestamp, crawl_date=crawl_date,
                    license_signal=license_signal,
                    quality_score=quality_score, quality_decision_method=quality_method,
                ))
            return records

        return process_unit

    while True:
        try:
            url, content = await asyncio.wait_for(content_queue.get(), timeout=poll_interval)
        except asyncio.TimeoutError:
            if frontier.quiescent.is_set():
                return
            continue

        queue_wait = timing.pop_queue_wait(url) if timing is not None else None
        score_started = time.perf_counter()
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
        finally:
            if timing is not None:
                timing.record("score", url=url, seconds=_since(score_started),
                              queue_wait_seconds=queue_wait)

        promote = score >= follow_threshold
        await frontier.score_and_resolve_children(url, score, promote)

        if score < extract_threshold:
            _record_page(timing, url, "skipped_extract", units=0, pairs=0, started=None)
            await frontier.mark_extract_outcome(url, "skipped_extract")
            await frontier.content_done(content_queue)
            continue

        try:
            units = await extraction_units_fn(content) if extraction_units_fn else [content]
        except Exception as e:
            _record_page(timing, url, "error", units=0, pairs=0, started=None)
            await frontier.mark_extract_outcome(url, "failed", f"extraction_units: {type(e).__name__}: {e}")
            await frontier.content_done(content_queue)
            continue

        page_title = extract_page_title(content)
        license_signal = detect_license_signal(content)
        section = derive_section(url, depth=section_depth, seed_prefixes=seed_prefixes)
        now = datetime.now(timezone.utc)
        timestamp, crawl_date = now.isoformat(), now.date().isoformat()

        extraction_started = time.perf_counter()

        process_unit = make_unit_processor(url, section, page_title, timestamp, crawl_date, license_signal)


        # unit_concurrency == 1 (config.EXTRACT_UNIT_CONCURRENCY's default):
        # strictly sequential, exactly the pre-concurrency behaviour -- a
        # failing unit stops the page before any later unit is called.
        # > 1: every unit runs concurrently under one semaphore; results land
        # in their chunk_index slot, so output order never depends on which
        # call finished first. The first failure cancels the sibling calls.
        unit_results: list[list[dict] | None] = []
        try:
            if unit_concurrency > 1:
                sem = asyncio.Semaphore(unit_concurrency)
                unit_results = await _run_in_order(
                    [functools.partial(process_unit, i, unit, sem) for i, unit in enumerate(units)]
                )
            else:
                for chunk_index, unit in enumerate(units):
                    unit_results.append(await process_unit(chunk_index, unit, None))
        except RateLimitError as e:
            _record_page(timing, url, "rate_limited", units=len(units),
                         pairs=sum(len(r) for r in unit_results if r), started=extraction_started)
            await frontier.mark_extract_outcome(
                url, "failed", "rate_limited (extraction)",
                backoff_seconds=e.retry_after or DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
            )
            await frontier.content_done(content_queue)
            continue
        except Exception as e:
            _record_page(timing, url, "error", units=len(units),
                         pairs=sum(len(r) for r in unit_results if r), started=extraction_started)
            await frontier.mark_extract_outcome(url, "failed", f"extraction: {type(e).__name__}: {e}")
            await frontier.content_done(content_queue)
            continue

        qa_pairs = [record for result in unit_results if result is not None for record in result]
        parsed_any_unit = any(result is not None for result in unit_results)

        if units and not parsed_any_unit:
            _record_page(timing, url, "malformed", units=len(units), pairs=0, started=extraction_started)
            await frontier.mark_extract_outcome(
                url, "failed", "malformed_json: no extraction unit produced valid JSON",
            )
            await frontier.content_done(content_queue)
            continue

        _record_page(timing, url, "ok", units=len(units), pairs=len(qa_pairs), started=extraction_started)
        chunks = chunk_fn(content) if chunk_fn is not None else None
        await frontier.put_results(results_queue, ExtractionResult(url=url, qa_pairs=qa_pairs, chunks=chunks))
        await frontier.content_done(content_queue)


async def map_worker(
    frontier: Frontier,
    content_queue: asyncio.Queue,
    poll_interval: float = POLL_INTERVAL,
) -> None:
    """Map-mode's extract-stage replacement: drains content_queue same as
    extract_worker, but does none of extract_worker's work -- no score_fn,
    no extract_fn, no chunking, no LLM calls at all. Every discovered
    in-scope child is promoted unconditionally (map mode has no relevance
    gate to promote against -- it's a "what's out there" pass, not a
    filtered one), and the page itself is marked 'mapped', a status
    distinct from skipped_extract so a map run's counts_by_status doesn't
    read as a mass relevance-gate rejection (see Frontier.mark_mapped).
    Content itself is never persisted -- same as extract_worker, it only
    ever lived in content_queue -- so "select this subtree for real
    extraction later" (Frontier.requeue_for_extraction) means a re-fetch,
    not a resume from saved content.
    """
    while True:
        try:
            url, _content = await asyncio.wait_for(content_queue.get(), timeout=poll_interval)
        except asyncio.TimeoutError:
            if frontier.quiescent.is_set():
                return
            continue
        await frontier.resolve_children(url, promote=True)
        await frontier.mark_mapped(url)
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
