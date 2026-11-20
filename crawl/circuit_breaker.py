"""LLM circuit breaker (config.LLM_BREAKER_THRESHOLD; Brief 1 3c item C).

Stops a run from spinning into an exhausted provider quota -- the real
741-consecutive-429 Gemini run (ROADMAP.md #54, LESSONS_LEARNED.md #81),
where every 429 released its page and the workers immediately called again.

States:
  closed     normal. Every call reports its outcome; any success resets the
             consecutive-429 count, N consecutive 429s trip the breaker.
  open       new LLM calls wait at the gate (acquire()) until the cooldown
             ends: the provider's own retry hint when it gave one, else
             base_cooldown doubling per trip, capped at max_cooldown.
  half_open  exactly one caller gets a probe permit; its success closes the
             breaker, its 429 trips it again (longer cooldown). Everyone
             else keeps waiting.
  stopped    terminal. After max_trips trips, or immediately on a daily-
             quota signal -- a quota hint naming a per-day quota, or an
             UNCAPPED provider retry hint of at least stop_if_retry_after
             seconds (RateLimitError.retry_after as reported; the 300s cap
             in crawl/pipeline.py applies only to a row's backoff). acquire()
             raises BreakerStopped; on_stop(reason) lets main.py end the run.

Deliberately NOT done: cancelling in-flight calls. The LLM call runs in
asyncio.to_thread, which can't be cancelled (ROADMAP.md #58) -- so the
breaker only gates NEW calls; calls already in flight finish normally and
report their outcomes (which update the count but never trigger a second
trip while the breaker is already open: only a probe's failure re-trips).

Waiting at the gate is a documented exception to CLAUDE.md's "rate limits
release the row, they don't sleep the worker": that rule exists so one
row's backoff doesn't idle a worker while other work could proceed; with
the breaker open, no LLM work can proceed, so waiting -- without burning
the pages' retry budgets -- is the point.

Pure asyncio, single event loop: no locks needed. The clock is injectable
for state-machine tests; waits use real asyncio timeouts.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

CLOSED, OPEN, HALF_OPEN, STOPPED = "closed", "open", "half_open", "stopped"
_PER_DAY = re.compile(r"per\s*_?\s*day|daily", re.IGNORECASE)


class BreakerStopped(Exception):
    """The breaker gave up on the provider; the run should stop (rows left
    resumable). Never a reason to mark a page failed."""


@dataclass(frozen=True)
class Permit:
    is_probe: bool = False


def is_daily_quota_signal(error, stop_if_retry_after: float) -> bool:
    """Decided from what the provider reported, never from a capped backoff."""
    hint = getattr(error, "quota_hint", None) or ""
    retry_after = getattr(error, "retry_after", None)
    return bool(_PER_DAY.search(hint)) or (retry_after is not None and retry_after >= stop_if_retry_after)


class LlmCircuitBreaker:
    def __init__(
        self,
        threshold: int,
        base_cooldown: float = 60.0,
        max_cooldown: float = 900.0,
        max_trips: int = 3,
        stop_if_retry_after: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[..., None] | None = None,
        on_stop: Callable[[str], None] | None = None,
    ):
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self.threshold = threshold
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown
        self.max_trips = max_trips
        self.stop_if_retry_after = stop_if_retry_after
        self._clock = clock
        self._on_event = on_event
        self._on_stop = on_stop

        self.state = CLOSED
        self.consecutive_rate_limits = 0
        self.trips = 0
        self.reopen_at: float | None = None
        self.stop_reason: str | None = None
        self._probe_out = False
        self._changed = asyncio.Event()

    # -- gate -----------------------------------------------------------
    async def acquire(self) -> Permit:
        """Wait until a new LLM call may start. Raises BreakerStopped."""
        while True:
            if self.state == STOPPED:
                raise BreakerStopped(self.stop_reason)
            if self.state == CLOSED:
                return Permit()
            if self.state == OPEN and self._clock() >= self.reopen_at:
                self._set(HALF_OPEN)
                self._emit("breaker_half_open", trips=self.trips)
            if self.state == HALF_OPEN and not self._probe_out:
                self._probe_out = True
                self._emit("breaker_probe", trips=self.trips)
                return Permit(is_probe=True)
            timeout = max(0.0, self.reopen_at - self._clock()) if self.state == OPEN else None
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), timeout=timeout)
            except TimeoutError:
                pass

    # -- outcomes ---------------------------------------------------------
    def success(self, permit: Permit) -> None:
        self.consecutive_rate_limits = 0
        if permit.is_probe and self.state == HALF_OPEN:
            self._probe_out = False
            self.trips = 0
            self._set(CLOSED)
            self._emit("breaker_close")

    def failure(self, permit: Permit, error: BaseException) -> bool:
        """Record a failed call. Returns True if the caller should go back to
        the gate and retry this same call (a rate limit while the breaker is
        open or stopping), False if it should handle the error as before (a
        non-rate-limit error, or a sporadic 429 with the breaker closed --
        the original release-the-row path)."""
        from crawl.pipeline import RateLimitError  # local: pipeline imports this module

        if not isinstance(error, RateLimitError):
            if permit.is_probe and self.state == HALF_OPEN:
                self._probe_out = False  # inconclusive: the next caller probes
                self._notify()
            return False

        self.consecutive_rate_limits += 1
        if is_daily_quota_signal(error, self.stop_if_retry_after):
            self._stop(f"provider quota exhausted (daily signal: quota_hint={error.quota_hint!r}, "
                       f"retry_after={error.retry_after!r})")
            return True
        if permit.is_probe and self.state == HALF_OPEN:
            self._probe_out = False
            self._trip(error)
            return True
        if self.state == CLOSED and self.consecutive_rate_limits >= self.threshold:
            self._trip(error)
            return True
        return self.state != CLOSED

    # -- internals ----------------------------------------------------------
    def _trip(self, error) -> None:
        self.trips += 1
        if self.trips > self.max_trips:
            self._stop(f"provider still rate-limiting after {self.max_trips} breaker trips")
            return
        backoff = min(self.base_cooldown * 2 ** (self.trips - 1), self.max_cooldown)
        hint = getattr(error, "retry_after", None) or 0.0
        cooldown = max(backoff, hint)  # the provider's own hint wins (below the stop threshold)
        self.reopen_at = self._clock() + cooldown
        self._set(OPEN)
        self._emit("breaker_open", trips=self.trips, cooldown_seconds=round(cooldown, 3),
                   consecutive_rate_limits=self.consecutive_rate_limits,
                   quota_hint=getattr(error, "quota_hint", None))

    def _stop(self, reason: str) -> None:
        if self.state == STOPPED:
            return
        self.stop_reason = reason
        self._set(STOPPED)
        self._emit("breaker_stop", reason=reason, trips=self.trips)
        if self._on_stop is not None:
            self._on_stop(reason)

    def _set(self, state: str) -> None:
        self.state = state
        self._notify()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _emit(self, kind: str, **fields) -> None:
        if self._on_event is not None:
            self._on_event(kind, **fields)
