"""Crawl / extract / write worker loops -- the real pipeline main.py runs
(proven end-to-end by step 8 Part D's live crawl and crash-resume test).

Every I/O dependency is injected (fetch_fn, score_fn, extract_fn, Writer,
chunk_fn) so these loops are fully testable against stubs -- see
tests/test_crawl_worker.py, tests/test_extract_worker.py.
"""
from __future__ import annotations

import asyncio
import hashlib
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
from crawl.scope import fetch_form, normalize_url
from crawl.provenance import call_served_model
from generators import PageContext
from crawl.timing import call_usage
from content.sectioning import DEFAULT_SECTION_DEPTH, derive_section
from storage.writer import Writer

DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 5.0
POLL_INTERVAL = 0.05


class FetchTimeout(Exception):
    pass


class FetchHTTPError(Exception):
    def __init__(self, status_code: int, retry_after: float | None = None):
        self.status_code = status_code
        # The site's Retry-After, if it sent one (used only with site backoff on).
        self.retry_after = retry_after
        super().__init__(f"HTTP {status_code}")

    @classmethod
    def from_response(cls, status_code: int, headers) -> FetchHTTPError:
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()} if hasattr(headers, "items") else {}
        return cls(status_code, retry_after=_retry_after_from_headers(lowered))


# Site answers that mean "slow down / try later" (ROADMAP #52): backed off when
# config.SITE_RETRY_BACKOFF is on, instead of re-fetched at once.
SITE_BACKOFF_STATUSES = (429, 503)
# Provider answers that mean "temporarily unavailable" (ROADMAP #51): released
# with a backoff when config.LLM_UNAVAILABLE_MAX_RELEASES > 0.
TRANSIENT_PROVIDER_STATUSES = (500, 502, 503, 504)


class ProviderUnavailableError(Exception):
    """An LLM provider answered 500/502/503/504 (e.g. Gemini's 503 "high
    demand"). Raised by main.py's extract_fn only when
    config.LLM_UNAVAILABLE_MAX_RELEASES > 0; extract_worker then releases the
    page with a backoff without using a retry, up to that cap per page."""

    def __init__(self, status_code: int | None = None, retry_after: float | None = None,
                 detail: str | None = None, **_ignored):
        self.status_code = status_code
        self.retry_after = retry_after
        self.detail = detail
        super().__init__(f"provider unavailable, status={status_code}")

    @classmethod
    def from_provider_error(cls, source) -> ProviderUnavailableError:
        return cls(**rate_limit_details(source))

    def details(self) -> dict:
        return {"status_code": self.status_code, "retry_after": self.retry_after, "detail": self.detail}


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


def doubling_backoff(attempt: int, hint: float | None) -> float:
    """The other side's own hint when given, else 5s doubling per attempt
    (attempt 0 -> 5s, 1 -> 10s, ...), always capped at
    RATE_LIMIT_MAX_BACKOFF_SECONDS (a far not_before keeps a run from
    quiescing)."""
    base = hint if hint else DEFAULT_RATE_LIMIT_BACKOFF_SECONDS * 2 ** max(0, attempt)
    return min(base, RATE_LIMIT_MAX_BACKOFF_SECONDS)


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
    if isinstance(e, ProviderUnavailableError):
        return "unavailable", {"unavailable": e.details()}
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
    site_backoff: bool = False,
    use_fetch_urls: bool = False,
    stop_on_site_429: bool = False,
) -> None:
    """site_backoff (config.SITE_RETRY_BACKOFF; off = today's immediate
    re-fetch): a site's 429/503 re-queues the page after doubling_backoff
    (the site's Retry-After, else 5s doubling per retry). Still a retry.

    use_fetch_urls (headless contract v2 only; ROADMAP #53): robots.txt is
    checked against, and the page fetched at, the form the URL was first
    discovered in (Frontier.fetch_url_for: e.g. with its trailing slash),
    and each child's request form is recorded. Row identity is unchanged.
    Off: exactly today's behaviour.

    stop_on_site_429 (headless contract v2 setting): the first 429 from the
    site fails that page (a counted retry, so it stays resumable), records a
    fatal error and stops the run via quiescence -- like a dead browser.
    Off: exactly today's behaviour."""
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

        target = await frontier.fetch_url_for(row.url) if use_fetch_urls else row.url
        policy = None
        if robots_cache is not None:
            policy = await robots_cache.get_policy(row.host)
            if not policy.is_allowed(target):
                print(f"[robots] disallowed by robots.txt, skipping: {target}")
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
                    html, raw_hrefs = await fetch_fn(target)
                else:
                    # Timed inside the politeness gate: fetch time only,
                    # not time spent waiting for the per-host slot.
                    fetch_started = time.perf_counter()
                    try:
                        html, raw_hrefs = await fetch_fn(target)
                    except Exception as e:
                        timing.record("fetch", url=row.url, seconds=_since(fetch_started),
                                      outcome=type(e).__name__)
                        raise
                    timing.record("fetch", url=row.url, seconds=_since(fetch_started), outcome="ok")
        except FetchTimeout as e:
            await frontier.mark_fetch_failed(row.url, f"timeout: {e}")
            continue
        except FetchHTTPError as e:
            if stop_on_site_429 and e.status_code == 429:
                await frontier.mark_fetch_failed(row.url, "http_429 (run stopped: stop_on_site_429)")
                msg = f"[FATAL] Site returned 429 for {target}; stopping the run (stop_on_site_429)."
                print(msg)
                if fatal_error_sink is not None:
                    fatal_error_sink.append(msg)
                frontier.quiescent.set()
                return
            if site_backoff and e.status_code in SITE_BACKOFF_STATUSES:
                backoff = doubling_backoff(row.retry_count, e.retry_after)
                await frontier.mark_fetch_failed(row.url, f"http_{e.status_code} (backoff {backoff:g}s)",
                                                 backoff_seconds=backoff)
            else:
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
        request_forms = []
        for href in raw_hrefs:
            normalized = normalize_url(href, target if use_fetch_urls else row.url)
            if normalized is None:
                continue
            if not scope_check(normalized):
                continue
            children.append((normalized, row.section_label))
            if use_fetch_urls:
                request_forms.append((normalized, fetch_form(href, target)))

        if request_forms:
            await frontier.record_fetch_urls(request_forms)
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
    llm_unavailable_max_releases: int = 0,
    record_builder: Callable[..., dict] | None = None,
    cost=None,
    balancer=None,
    generators: list | None = None,
    llm_call: Callable[[str, str], Awaitable[str]] | None = None,
    verifiers=None,
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
    marking the page failed (it stays in_progress, resumable).

    llm_unavailable_max_releases (config.LLM_UNAVAILABLE_MAX_RELEASES, 0 =
    off): a ProviderUnavailableError releases the page with doubling_backoff
    and no retry used, at most this many times per page per run
    (Frontier.release_llm_unavailable); the next one fails the page. The
    breaker neither counts nor resets on these: it only sees 429s.

    record_builder (headless contract v2 only): called as
    record_builder(fields, served_model=...) for each pair instead of
    build_canonical_record(**fields); served_model is what the provider's
    response said it ran for that unit's call (crawl.provenance). None:
    build_canonical_record, exactly as before.

    cost (headless contract v2 only, crawl.cost.CostLedger): every
    extraction and quality call attempt is recorded by outcome, with the
    token usage the response reported; 5xx page releases too. None: nothing
    recorded, exactly as before.

    balancer (headless contract v2 only, content.balance.Balancer): after
    the relevance gate, a page detected as low-value with action "skip" is
    marked skipped_extract with no extraction calls; after extraction, the
    page's rows are capped (per page, per source, low-value cap) before they
    go to the writer. None: no balancing, exactly as before.

    generators + llm_call (headless contract v2 only, generators/): instead
    of one extract_fn call per unit, every generator that wants the unit
    gets one llm_call(system_prompt, text) through the same breaker,
    semaphore, cost and timing path (timing events and cost rows carry the
    generator id), and record_builder is called with generator=. A unit
    fails as malformed only if every attempted generator's answer was
    unreadable. None: extract_fn, exactly as before.

    verifiers (headless contract v2 only, verifiers.VerifierRunner): a
    page's rows are verified after generation and before balancing; rows
    failing an on_fail=drop verifier are removed, the rest carry their
    verdicts. Rows finally sent to the writer are committed (near_duplicate's
    "seen"). None: no verification, exactly as before."""
    if generators is not None and (llm_call is None or record_builder is None):
        raise ValueError("generators need llm_call and record_builder")
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
                        if cost is not None:
                            cost.record("quality", _call_outcome(e)[0])
                        if timing is not None:
                            outcome, extra = _call_outcome(e)
                            timing.record(
                                "quality_call", url=url, chunk_index=chunk_index,
                                seconds=_since(quality_started), outcome=outcome, **extra,
                            )
                        if breaker is not None and breaker.failure(permit, e):
                            continue  # back to the gate; retry this same call
                        raise
                    if cost is not None:
                        cost.record("quality", "ok")
                    if timing is not None:
                        timing.record("quality_call", url=url, chunk_index=chunk_index,
                                      seconds=_since(quality_started), outcome="ok")
                if breaker is not None:
                    breaker.success(permit)
                return answer.value, answer.method

        async def guarded_call(chunk_index: int, unit: str, sem: asyncio.Semaphore | None, invoke,
                               role_extra: dict):
            """One LLM call behind the breaker gate and the unit semaphore,
            recorded in cost and timing on failure. Returns (raw response,
            served model, usage, started, seconds); the caller records the
            ok / malformed outcome once it has parsed the response."""
            while True:
                permit = await breaker.acquire() if breaker is not None else None
                if timing is not None or cost is not None:
                    call_usage.set(None)  # never attribute a previous call's usage to this one
                call_served_model.set(None)
                async with sem if sem is not None else nullcontext():
                    call_started = time.perf_counter()
                    try:
                        raw_response = await invoke()
                    except Exception as e:
                        outcome, extra = _call_outcome(e)
                        if cost is not None:
                            cost.record("extraction", outcome, **role_extra)
                        _record_extract_call(timing, url, chunk_index, unit, call_started, outcome,
                                             **extra, **role_extra)
                        if breaker is not None and breaker.failure(permit, e):
                            continue  # back to the gate; retry this same unit
                        raise
                    call_seconds = _since(call_started)
                    served_model = call_served_model.get()  # this call's, read in the same task
                    call_usage_now = call_usage.get() if cost is not None else None
                if breaker is not None:
                    breaker.success(permit)
                return raw_response, served_model, call_usage_now, call_started, call_seconds

        async def scored(chunk_index: int, pairs: list[dict], sem: asyncio.Semaphore | None) -> list:
            if quality_backend is None:
                return [(None, None)] * len(pairs)
            if sem is None:
                return [await score_pair(chunk_index, pair, None) for pair in pairs]
            return await _run_in_order([functools.partial(score_pair, chunk_index, pair, sem) for pair in pairs])

        def passes_quality(quality_score, quality_method) -> bool:
            if quality_backend is None:
                return True
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
            return quality_score >= active_threshold

        async def process_unit(chunk_index: int, unit: str, sem: asyncio.Semaphore | None) -> list[dict] | None:
            """One extraction unit: one extract_fn call, parse, optional
            quality scoring. Returns the unit's canonical records, or None if
            its response was malformed -- one bad unit doesn't fail the whole
            page (each unit is an independent LLM call; a page only fails
            below if literally none of its units parsed). Any other exception
            propagates. With a semaphore, a permit is held only around each
            LLM call -- never while waiting on this unit's own quality calls,
            which need permits too (holding one there could deadlock)."""
            raw_response, served_model, call_usage_now, call_started, call_seconds = await guarded_call(
                chunk_index, unit, sem, lambda: extract_fn(unit), {})
            try:
                pairs = parse_qa_json(raw_response)
            except MalformedExtractionError:
                if cost is not None:
                    cost.record("extraction", "malformed", call_usage_now)
                _record_extract_call(timing, url, chunk_index, unit, call_started, "malformed",
                                     seconds=call_seconds)
                return None
            if cost is not None:
                cost.record("extraction", "ok", call_usage_now)
            _record_extract_call(timing, url, chunk_index, unit, call_started, "ok", seconds=call_seconds)

            scores = await scored(chunk_index, pairs, sem)
            records = []
            for pair, (quality_score, quality_method) in zip(pairs, scores):
                if not passes_quality(quality_score, quality_method):
                    continue
                fields = {
                    "question": pair["instruction"], "answer": pair["response"],
                    "source_chunk": unit, "chunk_index": chunk_index, "source_url": url, "section": section,
                    "page_title": page_title, "generation_model": generation_model,
                    "extraction_strategy": extraction_strategy,
                    "timestamp": timestamp, "crawl_date": crawl_date,
                    "license_signal": license_signal,
                    "quality_score": quality_score, "quality_decision_method": quality_method,
                    "scraper_version": scraper_version,
                }
                if record_builder is None:
                    records.append(build_canonical_record(**fields))
                else:
                    records.append(record_builder(fields, served_model=served_model))
            return records

        async def process_unit_generators(chunk_index: int, unit: str, sem: asyncio.Semaphore | None,
                                          page: PageContext) -> list[dict] | None:
            """Contract v2: every configured generator that wants this unit
            gets one call (in config order). None only if every attempted
            generator's response was malformed; [] if none wanted the unit."""
            records, attempted, parsed = [], 0, 0
            for gen in generators:
                if not gen.wants(unit, page, chunk_index):
                    continue
                attempted += 1
                system_prompt, text = gen.request(unit, page, chunk_index)
                tag = {"generator": gen.id}
                try:
                    raw_response, served_model, call_usage_now, call_started, call_seconds = await guarded_call(
                        chunk_index, unit, sem, functools.partial(llm_call, system_prompt, text), tag)
                except ProviderUnavailableError as e:
                    e.generator = gen.id  # the page release below is booked against this generator
                    raise
                try:
                    items = gen.parse(raw_response, unit, page, chunk_index)
                except MalformedExtractionError:
                    if cost is not None:
                        cost.record("extraction", "malformed", call_usage_now, **tag)
                    _record_extract_call(timing, url, chunk_index, unit, call_started, "malformed",
                                         seconds=call_seconds, **tag)
                    continue
                parsed += 1
                if cost is not None:
                    cost.record("extraction", "ok", call_usage_now, **tag)
                _record_extract_call(timing, url, chunk_index, unit, call_started, "ok", seconds=call_seconds, **tag)
                scores = (await scored(chunk_index, [i["_pair"] for i in items], sem) if gen.quality_scored
                          else [(None, None)] * len(items))
                for item, (quality_score, quality_method) in zip(items, scores):
                    if gen.quality_scored and not passes_quality(quality_score, quality_method):
                        continue
                    fields = {k: v for k, v in item.items() if not k.startswith("_")}
                    fields.update({"source_chunk": unit, "chunk_index": chunk_index, "source_url": url,
                                   "section": section, "page_title": page_title})
                    if gen.generation_meta:
                        fields.update({"generation_model": generation_model,
                                       "extraction_strategy": extraction_strategy})
                    fields.update({"timestamp": timestamp, "crawl_date": crawl_date, "license_signal": license_signal,
                                   "quality_score": quality_score, "quality_decision_method": quality_method,
                                   "scraper_version": scraper_version})
                    records.append(record_builder(fields, served_model=served_model, generator=gen))
            return None if attempted and not parsed else records

        if generators is not None:
            return process_unit_generators
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

        low_value = balancer.classify(url, content, extract_page_title(content)) if balancer is not None else None
        if low_value is not None and low_value.action == "skip":
            _record_page(timing, url, "skipped_low_value", units=0, pairs=0, started=None)
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
        if generators is not None:
            process_unit = functools.partial(process_unit, page=PageContext(url, page_title, section, tuple(units)))


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
        except ProviderUnavailableError as e:
            pairs_so_far = sum(len(r) for r in unit_results if r)
            if llm_unavailable_max_releases > 0:
                if cost is not None:
                    cost.unavailable_release(generator=getattr(e, "generator", None))
                _record_page(timing, url, "unavailable", units=len(units), pairs=pairs_so_far,
                             started=extraction_started)
                await frontier.release_llm_unavailable(
                    url, f"llm_unavailable (extraction): HTTP {e.status_code}",
                    lambda n, hint=e.retry_after: doubling_backoff(n - 1, hint), llm_unavailable_max_releases,
                )
            else:
                _record_page(timing, url, "error", units=len(units), pairs=pairs_so_far, started=extraction_started)
                await frontier.mark_extract_outcome(url, "failed", f"extraction: {type(e).__name__}: {e}")
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
        if verifiers is not None:
            qa_pairs = await verifiers.run(url, qa_pairs)
        if balancer is not None:
            qa_pairs = balancer.apply(url, qa_pairs, low_value)
        if verifiers is not None:
            verifiers.commit(qa_pairs)
        chunks = chunk_fn(content) if chunk_fn is not None else None
        await frontier.put_results(results_queue, ExtractionResult(url=url, qa_pairs=qa_pairs, chunks=chunks))
        await frontier.content_done(content_queue)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def estimate_worker(
    frontier: Frontier,
    content_queue: asyncio.Queue,
    score_fn: ScoreFn,
    extract_threshold: float,
    follow_threshold: float,
    extraction_units_fn: Callable[[str], Awaitable[list[str]]] | None,
    pages: dict,
    poll_interval: float = POLL_INTERVAL,
    balancer=None,
    generators: list | None = None,
) -> None:
    """Dry pass (headless contract v2 `dry_run: true`): extract_worker's
    decisions without its LLM calls. Same score, same follow/promote, same
    extract_threshold skip, same extraction_units_fn -- so the pages and
    units match what a real run would extract -- then records each page's
    unit sizes in `pages` (url -> {"units": [chars, ...], "content_sha256",
    "unit_sha256": [...]} or {"skipped": reason}) and marks it 'mapped' (fetched, nothing extracted; it counts
    toward max_pages like 'done')."""
    while True:
        try:
            url, content = await asyncio.wait_for(content_queue.get(), timeout=poll_interval)
        except TimeoutError:
            if frontier.quiescent.is_set():
                return
            continue
        try:
            score = await score_fn(url, content)
        except Exception as e:  # noqa: BLE001 -- recorded; the dry pass goes on
            pages[url] = {"skipped": f"scoring failed: {type(e).__name__}"}
            await frontier.mark_extract_outcome(url, "failed", f"dry pass scoring: {type(e).__name__}: {e}")
            await frontier.content_done(content_queue)
            continue
        await frontier.score_and_resolve_children(url, score, score >= follow_threshold)
        if score < extract_threshold:
            pages[url] = {"skipped": f"relevance {score:.3f} below extract_threshold {extract_threshold}"}
            await frontier.mark_extract_outcome(url, "skipped_extract")
            await frontier.content_done(content_queue)
            continue
        low_value = balancer.classify(url, content, extract_page_title(content)) if balancer is not None else None
        if low_value is not None and low_value.action == "skip":
            pages[url] = {"skipped": low_value.describe()}
            await frontier.mark_extract_outcome(url, "skipped_extract")
            await frontier.content_done(content_queue)
            continue
        units = await extraction_units_fn(content) if extraction_units_fn else [content]
        pages[url] = {"units": [len(u) for u in units], "content_sha256": _sha256(content),
                      "unit_sha256": [_sha256(u) for u in units]}
        if generators is not None:  # v2: the calls each generator would make, as (system, text) lengths
            page = PageContext(url, extract_page_title(content), None, tuple(units))
            pages[url]["calls"] = {
                gen.id: [tuple(map(len, gen.request(u, page, i))) for i, u in enumerate(units)
                         if gen.wants(u, page, i)]
                for gen in generators}
        await frontier.mark_mapped(url)
        await frontier.content_done(content_queue)


ITEMS_RECORD_SCHEMA_VERSION = 1


def page_record(url: str, content: str, *, fetch_url: str, info: dict, score: float | None,
                fetched_at: str, scraper_version: str | None, run_id: str | None) -> dict:
    """Items mode's one record per page: the cleaned markdown exactly as the
    extract stage would have received it (chrome-stripped), its SHA-256 (the
    same hash the dry pass records as page_fingerprints[url].content_sha256),
    and fetch metadata. No chunking: that's the consumer's job."""
    return {"record_type": "page", "schema_version": ITEMS_RECORD_SCHEMA_VERSION, "url": url,
            "fetch_url": fetch_url, "title": extract_page_title(content), "markdown": content,
            "content_sha256": _sha256(content), "chars": len(content), "fetched_at": fetched_at,
            "depth": info.get("depth"), "parent_url": info.get("parent_url"), "relevance_score": score,
            "scraper_version": scraper_version, "run_id": run_id}


async def items_worker(
    frontier: Frontier,
    content_queue: asyncio.Queue,
    score_fn: ScoreFn,
    extract_threshold: float,
    follow_threshold: float,
    write_record: Callable[[dict], Awaitable[None]],
    written_urls: set,
    report: dict,
    clock: Callable[[], str],
    scraper_version: str | None = None,
    run_id: str | None = None,
    poll_interval: float = POLL_INTERVAL,
) -> None:
    """Items mode (headless contract v2 `items_mode: true`): extract_worker's
    score / follow / extract_threshold decisions, then one page record via
    `write_record` (an atomic append, run off the event loop) instead of any
    LLM work. No generators, verifiers, balancing or chunking. A URL already
    in `written_urls` (a resumed run) is not written twice. The page ends
    'mapped' (fetched, nothing extracted; it counts toward max_pages)."""
    while True:
        try:
            url, content = await asyncio.wait_for(content_queue.get(), timeout=poll_interval)
        except TimeoutError:
            if frontier.quiescent.is_set():
                return
            continue
        try:
            score = await score_fn(url, content)
        except Exception as e:  # noqa: BLE001 -- recorded; the run goes on
            report["skipped"][url] = f"scoring failed: {type(e).__name__}"
            await frontier.mark_extract_outcome(url, "failed", f"items scoring: {type(e).__name__}: {e}")
            await frontier.content_done(content_queue)
            continue
        await frontier.score_and_resolve_children(url, score, score >= follow_threshold)
        if score < extract_threshold:
            report["skipped"][url] = f"relevance {score:.3f} below extract_threshold {extract_threshold}"
            await frontier.mark_extract_outcome(url, "skipped_extract")
            await frontier.content_done(content_queue)
            continue
        if url in written_urls:
            report["already_written"] += 1
        else:
            record = page_record(url, content, fetch_url=await frontier.fetch_url_for(url),
                                 info=await frontier.page_info(url), score=score, fetched_at=clock(),
                                 scraper_version=scraper_version, run_id=run_id)
            try:
                await write_record(record)
            except OSError as e:
                await frontier.mark_write_failed(url, f"items write: {type(e).__name__}: {e}")
                await frontier.content_done(content_queue)
                continue
            written_urls.add(url)
            report["pages_written"] += 1
        await frontier.mark_mapped(url)
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
