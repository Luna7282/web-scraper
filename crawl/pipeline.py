"""Crawl / extract / write worker loops -- the real pipeline main.py runs
(proven end-to-end by step 8 Part D's live crawl and crash-resume test).

Every I/O dependency is injected (fetch_fn, score_fn, extract_fn, Writer,
chunk_fn) so these loops are fully testable against stubs -- see
tests/test_crawl_worker.py, tests/test_extract_worker.py.
"""
from __future__ import annotations

import asyncio
import functools
import math
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from crawl.timing import TimingLog
    from decisions import DecisionBackend

from storage.canonical import build_canonical_record, detect_license_signal, extract_page_title
from config import MAX_RETRIES, RATE_LIMIT_MAX_BACKOFF_SECONDS, get_threshold
from content.extraction import MalformedExtractionError, parse_qa_json
from crawl.circuit_breaker import BreakerStopped, LlmCircuitBreaker
from crawl.frontier import ROBOTS_DISALLOWED_ERROR, Frontier, FrontierRow
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
    """A provider said "slow down" (429, or Jev's 529). Carries what the
    provider told us about it -- see rate_limit_details() -- so the backoff
    can use the provider's own retry hint and the stored error says which
    quota was hit (ROADMAP.md #51/#56). Every field is optional: a bare
    RateLimitError() still means "back off by the default"."""

    def __init__(
        self, retry_after: float | None = None, status_code: int | None = None,
        quota_hint: str | None = None, detail: str | None = None,
    ):
        self.retry_after = retry_after
        self.status_code = status_code
        self.quota_hint = quota_hint
        self.detail = detail
        super().__init__(f"rate limited, retry_after={retry_after}")

    @classmethod
    def from_provider_error(cls, source) -> RateLimitError:
        """Build from an openai.APIStatusError (or anything with .response/
        .body) or a raw requests/httpx response. Never raises."""
        return cls(**rate_limit_details(source))

    def summary(self) -> str:
        """Short, bounded, secret-redacted text for frontier.last_error."""
        return self.quota_hint or self.detail or ""

    def details(self) -> dict:
        return {"status_code": self.status_code, "retry_after": self.retry_after,
                "quota_hint": self.quota_hint, "detail": self.detail}


# --- 429 parsing (ROADMAP.md #56) -------------------------------------------
# Same key shapes .githooks/pre-commit blocks, plus bearer tokens: anything
# that looks like a credential is replaced before an error excerpt can reach
# frontier.db or timings.jsonl.
_SECRET_SHAPED = re.compile(
    r"nvapi-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,}"
    r"|[A-Za-z0-9]{32,}\.[A-Za-z0-9_-]{16,}|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"
)
RATE_LIMIT_DETAIL_MAX_CHARS = 300
_GOOGLE_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def _redact_and_cap(text: str) -> str:
    text = _SECRET_SHAPED.sub("[redacted]", " ".join(text.split()))
    if len(text) > RATE_LIMIT_DETAIL_MAX_CHARS:
        text = text[: RATE_LIMIT_DETAIL_MAX_CHARS - 1] + "…"
    return text


def _positive_finite(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _header(headers, name: str) -> str | None:
    try:
        value = headers.get(name)
    except Exception:  # noqa: BLE001 -- a malformed/mock headers object must not break error handling
        return None
    return value if isinstance(value, str) else None


def _retry_after_from_headers(headers) -> float | None:
    """retry-after-ms, then Retry-After as seconds or an HTTP date -- the
    same precedence the openai SDK itself uses."""
    ms = _positive_finite(_header(headers, "retry-after-ms"))
    if ms is not None:
        return ms / 1000
    raw = _header(headers, "retry-after")
    if raw is None:
        return None
    seconds = _positive_finite(raw)
    if seconds is not None:
        return seconds
    try:
        from email.utils import parsedate_to_datetime
        delta = parsedate_to_datetime(raw).timestamp() - time.time()
    except (TypeError, ValueError, OverflowError):
        return None
    return delta if delta > 0 else None


def _walk(node, depth: int = 0):
    """Every dict in a parsed error body (Gemini's OpenAI-compatible
    endpoint returns a list of {"error": ...}; the openai SDK may pass the
    inner error object or the whole body). Depth-capped."""
    if depth > 6:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1)


def rate_limit_details(source) -> dict:
    """What a provider's rate-limit response says: status_code,
    retry_after (seconds: retry-after-ms / Retry-After headers first, then
    a Google RetryInfo `retryDelay` like "12s" in the body), quota_hint
    (Google QuotaFailure quotaMetric/quotaId -- names per-minute vs
    per-day and free-tier quotas), detail (the provider's message,
    whitespace-collapsed, secret-redacted, capped). Headers are never
    copied. Never raises: unknown shapes just yield Nones."""
    response = getattr(source, "response", None) or source
    status = getattr(source, "status_code", None) or getattr(response, "status_code", None)
    headers = getattr(response, "headers", None)
    body = getattr(source, "body", None)
    if body is None and response is not None and not isinstance(response, (str, bytes)):
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 -- not JSON / mock / already consumed: no body
            body = None

    retry_after = _retry_after_from_headers(headers) if headers is not None else None
    quota, message = [], None
    for node in _walk(body):
        if retry_after is None and isinstance(node.get("retryDelay"), str):
            m = _GOOGLE_DURATION.match(node["retryDelay"])
            if m:
                retry_after = _positive_finite(m.group(1))
        for key in ("quotaMetric", "quotaId"):
            if isinstance(node.get(key), str) and node[key] not in quota:
                quota.append(node[key])
        if message is None and isinstance(node.get("message"), str):
            message = node["message"]
    if message is None and isinstance(body, str):
        message = body
    return {
        "status_code": status if isinstance(status, int) else None,
        "retry_after": retry_after,
        "quota_hint": _redact_and_cap(", ".join(quota)) if quota else None,
        "detail": _redact_and_cap(message) if message else None,
    }


def _rate_limit_backoff(e: RateLimitError) -> float:
    """The provider's own hint when it gave one, else the old flat default;
    capped so a per-day retryDelay can't park a row for hours (a queued row
    with a far not_before keeps the run from ever reaching quiescence)."""
    return min(e.retry_after or DEFAULT_RATE_LIMIT_BACKOFF_SECONDS, RATE_LIMIT_MAX_BACKOFF_SECONDS)


def _rate_limit_reason(stage: str, e: RateLimitError) -> str:
    summary = e.summary()
    return f"rate_limited ({stage}): {summary}" if summary else f"rate_limited ({stage})"


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
        # BreakerStopped first (the run is ending; never mark the page failed),
        # then RateLimitError (its own retry-later path), then the first error.
        for kind in (BreakerStopped, RateLimitError):
            match = next((e for e in leaves if isinstance(e, kind)), None)
            if match is not None:
                raise match from None
        raise leaves[0] from None
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


def _call_outcome(e: Exception) -> tuple[str, dict]:
    """(outcome, extra timing fields) for a failed LLM call."""
    if isinstance(e, RateLimitError):
        return "rate_limited", {"rate_limit": e.details()}
    return "error", {"error": type(e).__name__}


def _record_extract_call(
    timing: TimingLog | None, url: str, chunk_index: int, unit: str, started: float,
    outcome: str, seconds: float | None = None, **extra,
) -> None:
    if timing is None:
        return
    timing.record(
        "extract_call", url=url, chunk_index=chunk_index, chars_in=len(unit),
        seconds=seconds if seconds is not None else _since(started),
        outcome=outcome, usage=call_usage.get(), **extra, **timing.llm_fields(),
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
                await frontier.mark_permanently_failed(row.url, ROBOTS_DISALLOWED_ERROR)
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
    breaker: LlmCircuitBreaker | None = None,
    scraper_version: str | None = None,
) -> None:
    """unit_concurrency (config.EXTRACT_UNIT_CONCURRENCY, default 1): how
    many of one page's extraction units -- and, with a quality_backend, its
    pair-scoring calls -- may be in flight at once. 1 is the original
    strictly-sequential behaviour; see the comment at the extraction loop.

    breaker (config.LLM_BREAKER_THRESHOLD, off by default; crawl/
    circuit_breaker.py): every extraction/quality LLM call first acquires a
    permit -- outside the semaphore, so a waiting call holds no slot -- and
    reports its outcome. A rate limit while the breaker is open sends that
    same call back to the gate (finished units are kept, the page's retry
    budget isn't spent); a sporadic 429 with the breaker closed takes the
    original release-the-row path. BreakerStopped ends this worker without
    marking the page failed (it stays in_progress, resumable)."""
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
            while True:
                permit = await breaker.acquire() if breaker is not None else None
                async with sem if sem is not None else nullcontext():
                    quality_started = time.perf_counter()
                    try:
                        answer = await quality_backend.noul(
                            f"Instruction: {pair['instruction']}\nResponse: {pair['response']}",
                            "Is this a good instruction-response training pair?",
                        )
                    except Exception as e:
                        if timing is not None:
                            outcome, extra = _call_outcome(e)
                            timing.record(
                                "quality_call", url=url, chunk_index=chunk_index,
                                seconds=_since(quality_started), outcome=outcome, **extra,
                            )
                        if breaker is not None and breaker.failure(permit, e):
                            continue  # back to the gate; retry this same call
                        raise
                    if timing is not None:
                        timing.record("quality_call", url=url, chunk_index=chunk_index,
                                      seconds=_since(quality_started), outcome="ok")
                if breaker is not None:
                    breaker.success(permit)
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
            while True:
                permit = await breaker.acquire() if breaker is not None else None
                if timing is not None:
                    call_usage.set(None)  # never attribute a previous call's usage to this one
                async with sem if sem is not None else nullcontext():
                    call_started = time.perf_counter()
                    try:
                        raw_response = await extract_fn(unit)
                    except Exception as e:
                        outcome, extra = _call_outcome(e)
                        _record_extract_call(timing, url, chunk_index, unit, call_started, outcome, **extra)
                        if breaker is not None and breaker.failure(permit, e):
                            continue  # back to the gate; retry this same unit
                        raise
                    call_seconds = _since(call_started)
                if breaker is not None:
                    breaker.success(permit)
                break
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
                    scraper_version=scraper_version,
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
                url, "failed", _rate_limit_reason("scoring", e), backoff_seconds=_rate_limit_backoff(e),
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
        except BreakerStopped:
            # The run is ending (main.py's on_stop sets quiescence). Leave
            # the row in_progress -- recover_crashed() requeues it on resume
            # -- and never count this as a page failure.
            _record_page(timing, url, "breaker_stopped", units=len(units),
                         pairs=sum(len(r) for r in unit_results if r), started=extraction_started)
            return
        except RateLimitError as e:
            _record_page(timing, url, "rate_limited", units=len(units),
                         pairs=sum(len(r) for r in unit_results if r), started=extraction_started)
            await frontier.mark_extract_outcome(
                url, "failed", _rate_limit_reason("extraction", e), backoff_seconds=_rate_limit_backoff(e),
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
