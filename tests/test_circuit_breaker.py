"""LLM circuit breaker (crawl/circuit_breaker.py; config.LLM_BREAKER_THRESHOLD;
Brief 1 3c item C) -- offline. A real 429 under concurrency has never been
observed, so a scripted StubProvider plays the provider: N good calls, then
rate limits forever, for a time window, or with a per-day / "retry in 20
hours" hint.

Covers the state machine (trip, cooldown from the provider hint or doubling,
single half-open probe, max trips, daily-quota stop from the UNCAPPED hint,
neutral non-429 errors, in-flight outcomes never re-trip), the pipeline
integration (gate outside the semaphore, finished units kept, in-flight
calls never cancelled, sporadic 429 keeps the old path, BreakerStopped never
fails a page), the SDK max_retries switch, and the 741-429 spiral as a
regression test: reproduced without the breaker, bounded with it.
"""
import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import config
from crawl.circuit_breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    STOPPED,
    BreakerStopped,
    LlmCircuitBreaker,
    Permit,
    is_daily_quota_signal,
)
from crawl.frontier import Frontier
from crawl.pipeline import RateLimitError, crawl_worker, extract_worker, writer_worker
from storage.writer import Writer

PAIR = '[{"instruction": "Q", "response": "A"}]'


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def rl(retry_after=None, quota_hint=None):
    return RateLimitError(retry_after=retry_after, quota_hint=quota_hint)


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------
class TestStateMachine(unittest.IsolatedAsyncioTestCase):
    def make(self, **kw):
        self.events, self.stops = [], []
        self.clock = FakeClock()
        args = {"threshold": 3, "base_cooldown": 60, "max_cooldown": 900, "max_trips": 3,
                "stop_if_retry_after": 3600, "clock": self.clock,
                "on_event": lambda k, **f: self.events.append((k, f)), "on_stop": self.stops.append}
        args.update(kw)
        return LlmCircuitBreaker(**args)

    async def test_success_resets_the_streak_and_n_consecutive_trip(self):
        b = self.make()
        p = await b.acquire()
        b.failure(p, rl())
        b.failure(p, rl())
        b.success(p)
        self.assertEqual((b.state, b.consecutive_rate_limits), (CLOSED, 0))
        self.assertFalse(b.failure(p, rl()))  # 1: sporadic -> original release-the-row path
        self.assertFalse(b.failure(p, rl()))  # 2
        self.assertTrue(b.failure(p, rl()))   # 3: trips, caller retries at the gate
        self.assertEqual(b.state, OPEN)
        self.assertEqual(b.reopen_at, self.clock.now + 60)

    async def test_cooldown_doubles_per_trip_and_the_provider_hint_wins(self):
        b = self.make(threshold=1)
        b.failure(Permit(), rl())
        self.assertEqual(b.reopen_at - self.clock.now, 60)
        self.clock.now = b.reopen_at
        probe = await b.acquire()
        b.failure(probe, rl())  # probe fails: trip 2
        self.assertEqual(b.reopen_at - self.clock.now, 120)
        self.clock.now = b.reopen_at
        probe = await b.acquire()
        b.failure(probe, rl(retry_after=500))  # trip 3: hint 500 > backoff 240
        self.assertEqual(b.reopen_at - self.clock.now, 500)

    async def test_half_open_gives_exactly_one_probe_and_its_success_closes(self):
        b = self.make(threshold=1)
        b.failure(Permit(), rl())
        self.clock.now = b.reopen_at
        probe = await b.acquire()
        self.assertTrue(probe.is_probe)
        self.assertEqual(b.state, HALF_OPEN)
        waiter = asyncio.create_task(b.acquire())
        await asyncio.sleep(0.02)
        self.assertFalse(waiter.done())  # second caller waits for the probe's verdict
        b.success(probe)
        permit = await asyncio.wait_for(waiter, 1)
        self.assertFalse(permit.is_probe)
        self.assertEqual((b.state, b.trips), (CLOSED, 0))

    async def test_max_trips_stops_and_acquire_raises(self):
        b = self.make(threshold=1, max_trips=2)
        b.failure(Permit(), rl())
        for _ in range(2):
            self.clock.now = b.reopen_at
            b.failure(await b.acquire(), rl())
        self.assertEqual(b.state, STOPPED)
        self.assertEqual(len(self.stops), 1)
        with self.assertRaises(BreakerStopped):
            await b.acquire()

    async def test_daily_quota_hint_stops_on_the_first_429(self):
        b = self.make(threshold=5)
        stop = b.failure(Permit(), rl(retry_after=41, quota_hint="GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
        self.assertTrue(stop)
        self.assertEqual(b.state, STOPPED)
        self.assertIn("daily", self.stops[0])

    async def test_retry_in_20_hours_stops_immediately_from_the_uncapped_hint(self):
        """The row backoff caps at RATE_LIMIT_MAX_BACKOFF_SECONDS (300s);
        the breaker must decide from the provider's raw 72,000s, not loop
        every 300s."""
        b = self.make(threshold=5)
        self.assertGreater(72_000, config.RATE_LIMIT_MAX_BACKOFF_SECONDS)
        b.failure(Permit(), rl(retry_after=72_000))
        self.assertEqual(b.state, STOPPED)
        self.assertIn("72000", self.stops[0])

    def test_daily_signal_rules(self):
        self.assertTrue(is_daily_quota_signal(rl(quota_hint="requests per day"), 3600))
        self.assertTrue(is_daily_quota_signal(rl(quota_hint="daily_limit"), 3600))
        self.assertTrue(is_daily_quota_signal(rl(retry_after=3600), 3600))
        self.assertFalse(is_daily_quota_signal(rl(retry_after=300), 3600))  # the cap value is not "daily"
        self.assertFalse(is_daily_quota_signal(rl(quota_hint="PerMinutePerProject"), 3600))
        self.assertFalse(is_daily_quota_signal(rl(), 3600))

    async def test_non_rate_limit_errors_are_neutral_and_free_the_probe(self):
        b = self.make(threshold=2)
        p = await b.acquire()
        b.failure(p, rl())
        self.assertFalse(b.failure(p, ValueError("boom")))
        self.assertEqual(b.consecutive_rate_limits, 1)  # neither reset nor counted
        b.failure(p, rl())
        self.clock.now = b.reopen_at
        probe = await b.acquire()
        b.failure(probe, ValueError("500"))  # inconclusive probe
        self.assertEqual(b.state, HALF_OPEN)
        self.assertTrue((await b.acquire()).is_probe)  # the next caller probes

    async def test_in_flight_outcomes_while_open_never_re_trip(self):
        b = self.make(threshold=1)
        old = await b.acquire()  # a call started before the trip
        b.failure(Permit(), rl())
        reopen = b.reopen_at
        self.assertTrue(b.failure(old, rl()))  # finishes 429 while open: back to the gate...
        self.assertEqual((b.state, b.trips, b.reopen_at), (OPEN, 1, reopen))  # ...but no second trip

    def test_threshold_must_be_positive(self):
        with self.assertRaises(ValueError):
            LlmCircuitBreaker(threshold=0)


# ---------------------------------------------------------------------------
# config: SDK retries
# ---------------------------------------------------------------------------
class TestSdkMaxRetries(unittest.TestCase):
    def test_default_unchanged_and_forced_to_zero_with_breaker(self):
        with patch.object(config, "LLM_BREAKER_THRESHOLD", 0), patch.object(config, "LLM_SDK_MAX_RETRIES", 2):
            self.assertEqual(config.effective_llm_sdk_max_retries(), 2)
        with patch.object(config, "LLM_BREAKER_THRESHOLD", 5), patch.object(config, "LLM_SDK_MAX_RETRIES", 2):
            self.assertEqual(config.effective_llm_sdk_max_retries(), 0)

    def test_get_llm_passes_it_to_the_sdk_client(self):
        import llm_factory
        llm, _ = llm_factory.get_llm("ollama", "stub-model")
        self.assertEqual(llm.root_client.max_retries, config.effective_llm_sdk_max_retries())
        with patch.object(llm_factory, "effective_llm_sdk_max_retries", return_value=0):
            llm, _ = llm_factory.get_llm("ollama", "stub-model")
        self.assertEqual(llm.root_client.max_retries, 0)


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
class StubProvider:
    """ok_calls good calls, then: 'forever' (429 every call), 'window' (429
    for window seconds, then good again). retry_after/quota_hint ride on each
    RateLimitError like a real provider's hint."""

    def __init__(self, ok_calls, mode="forever", window=0.0, retry_after=0.01, quota_hint=None, latency=0.002):
        self.ok_calls, self.mode, self.window = ok_calls, mode, window
        self.retry_after, self.quota_hint, self.latency = retry_after, quota_hint, latency
        self.calls = self.rate_limited = 0
        self.limited_until = None
        self.calls_by_unit = {}

    async def extract_fn(self, unit):
        self.calls += 1
        n = self.calls  # this call's number, before other concurrent calls bump the counter
        self.calls_by_unit[unit] = self.calls_by_unit.get(unit, 0) + 1
        await asyncio.sleep(self.latency)
        if n > self.ok_calls:
            if self.mode == "forever":
                self.rate_limited += 1
                raise RateLimitError(retry_after=self.retry_after, quota_hint=self.quota_hint)
            if self.limited_until is None:
                self.limited_until = time.monotonic() + self.window
            if time.monotonic() < self.limited_until:
                self.rate_limited += 1
                raise RateLimitError(retry_after=self.retry_after, quota_hint=self.quota_hint)
        return PAIR


class PipelineCase(unittest.IsolatedAsyncioTestCase):
    N_SEEDS = 30

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.frontier = Frontier(":memory:", max_pages=5, max_retries=3)
        await self.frontier.open()
        await self.frontier.recover_crashed()
        await self.frontier.seed([(f"https://x.test/p{i}", None) for i in range(self.N_SEEDS)])
        self.fetches = 0
        self.fatal = []

    async def asyncTearDown(self):
        await self.frontier.close()
        self.tmp.cleanup()

    def breaker(self, **kw):
        def on_stop(reason):
            self.fatal.append(reason)
            self.frontier.quiescent.set()
        self.breaker_events = []
        args = {"threshold": 3, "base_cooldown": 0.05, "max_cooldown": 0.4, "max_trips": 3,
                "stop_if_retry_after": 3600, "on_stop": on_stop,
                "on_event": lambda k, **f: self.breaker_events.append(k)}
        args.update(kw)
        return LlmCircuitBreaker(**args)

    async def run_pipeline(self, provider, breaker=None, units=("a", "b"), concurrency=1, timeout=15):
        async def fetch(url):
            self.fetches += 1
            return "content", []

        async def units_fn(content):
            return list(units)

        async def score_fn(url, content):
            return 1.0

        cq, rq = asyncio.Queue(maxsize=4), asyncio.Queue(maxsize=4)
        writer = Writer(os.path.join(self.tmp.name, "canonical.jsonl"))
        tasks = [asyncio.create_task(crawl_worker(self.frontier, fetch, cq, lambda u: True, poll_interval=0.005))
                 for _ in range(2)]
        tasks += [asyncio.create_task(extract_worker(
            self.frontier, cq, rq, score_fn, provider.extract_fn, 0.5, 0.5, extraction_units_fn=units_fn,
            poll_interval=0.005, unit_concurrency=concurrency, breaker=breaker)) for _ in range(2)]
        tasks.append(asyncio.create_task(writer_worker(self.frontier, rq, writer, poll_interval=0.005)))
        started = time.monotonic()
        try:
            await asyncio.wait_for(self.frontier.quiescent.wait(), timeout=timeout)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return time.monotonic() - started

    async def failed_rows(self):
        cur = await self.frontier._conn.execute("SELECT last_error FROM frontier WHERE status='failed'")
        return [r[0] for r in await cur.fetchall()]


class TestSpiralRegression(PipelineCase):
    async def test_without_breaker_the_741_429_spiral_reproduces(self):
        """Characterisation of the real Gemini run: quota gone after a few
        calls, every 429 releases its page, failed pages free their budget
        slots, and the run burns through every page it can find."""
        provider = StubProvider(ok_calls=3, mode="forever")
        await self.run_pipeline(provider, breaker=None)
        counts = await self.frontier.counts_by_status()
        # The 3 good calls can finish at most one 2-unit page; every other page
        # fails -- far past the budget of 5 -- each re-fetched per retry.
        self.assertEqual(counts.get("done", 0) + counts.get("failed", 0), self.N_SEEDS)
        self.assertGreaterEqual(counts.get("failed", 0), self.N_SEEDS - 2)
        self.assertGreaterEqual(self.fetches, (self.N_SEEDS - 2) * 3)
        self.assertGreaterEqual(provider.rate_limited, (self.N_SEEDS - 2) * 3)

    async def test_with_breaker_the_same_provider_is_bounded_and_stops_cleanly(self):
        provider = StubProvider(ok_calls=3, mode="forever")
        await self.run_pipeline(provider, breaker=self.breaker())
        self.assertEqual(len(self.fatal), 1)
        self.assertIn("breaker trips", self.fatal[0])
        # threshold (3) + up to one in-flight call per extract worker + one probe per trip
        self.assertLessEqual(provider.rate_limited, 3 + 2 + 3 + 2)
        self.assertEqual(await self.failed_rows(), [])  # no page burned by rate limiting
        # pages in crawl workers (2) + content_queue (4) + held at the gate by
        # extract workers (2) + the page finished on the good calls and its
        # successor (2), plus one released before the trip -- vs ~90 without.
        self.assertLessEqual(self.fetches, 2 + 4 + 2 + 2 + 1)
        counts = await self.frontier.counts_by_status()
        self.assertGreater(counts.get("queued", 0) + counts.get("in_progress", 0), 0)  # resumable
        self.assertIn("breaker_open", self.breaker_events)
        self.assertIn("breaker_stop", self.breaker_events)


class TestBreakerInThePipeline(PipelineCase):
    async def test_quota_window_recovers_and_the_run_completes(self):
        provider = StubProvider(ok_calls=3, mode="window", window=0.15)
        await self.run_pipeline(provider, breaker=self.breaker(threshold=2))
        counts = await self.frontier.counts_by_status()
        self.assertGreaterEqual(counts.get("done", 0), 5)
        self.assertEqual(await self.failed_rows(), [])
        self.assertEqual(self.fatal, [])
        self.assertIn("breaker_close", self.breaker_events)
        self.assertLessEqual(provider.rate_limited, 12)

    async def test_retry_in_20_hours_stops_the_run_at_once(self):
        provider = StubProvider(ok_calls=3, mode="forever", retry_after=72_000)
        elapsed = await self.run_pipeline(provider, breaker=self.breaker(threshold=5))
        self.assertLess(elapsed, 2)  # not one 300s loop, let alone many
        self.assertEqual(len(self.fatal), 1)
        self.assertIn("72000", self.fatal[0])
        self.assertLessEqual(provider.rate_limited, 2)  # the first one (plus one in flight)
        self.assertEqual(await self.failed_rows(), [])

    async def test_in_flight_siblings_are_not_cancelled_and_finished_units_are_kept(self):
        class TripOnFirstUnit(StubProvider):
            async def extract_fn(self, unit):
                self.calls_by_unit[unit] = self.calls_by_unit.get(unit, 0) + 1
                if unit == "u0" and self.calls_by_unit[unit] == 1:
                    await asyncio.sleep(0.005)
                    self.rate_limited += 1
                    raise RateLimitError(retry_after=None)
                await asyncio.sleep(0.05)  # siblings still in flight when u0 trips
                return PAIR

        provider = TripOnFirstUnit(ok_calls=0)
        await self.frontier.close()
        self.frontier = Frontier(":memory:", max_pages=1, max_retries=3)
        await self.frontier.open()
        await self.frontier.recover_crashed()
        await self.frontier.seed([("https://x.test/only", None)])
        await self.run_pipeline(provider, breaker=self.breaker(threshold=1),
                       units=("u0", "u1", "u2", "u3"), concurrency=4)
        self.assertEqual(await self.frontier.counts_by_status(), {"done": 1})
        self.assertEqual(provider.calls_by_unit, {"u0": 2, "u1": 1, "u2": 1, "u3": 1})
        self.assertEqual(self.fetches, 1)  # never released and re-fetched

    async def test_sporadic_429_with_breaker_closed_keeps_the_release_path(self):
        provider = StubProvider(ok_calls=10_000)
        original = provider.extract_fn
        state = {"n": 0}

        async def one_429(unit):
            state["n"] += 1
            if state["n"] == 1:
                raise RateLimitError(retry_after=0.01)
            return await original(unit)

        provider.extract_fn = one_429
        await self.run_pipeline(provider, breaker=self.breaker(threshold=3))
        counts = await self.frontier.counts_by_status()
        self.assertGreaterEqual(counts.get("done", 0), 5)
        self.assertEqual(self.fatal, [])
        self.assertEqual(self.breaker_events, [])  # one 429 below threshold never opens it
        self.assertGreaterEqual(self.fetches, 6)  # that page was released and re-fetched (old path)


if __name__ == "__main__":
    unittest.main()
