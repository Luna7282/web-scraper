"""Concurrent per-page unit extraction (config.EXTRACT_UNIT_CONCURRENCY,
crawl/pipeline.py extract_worker's unit_concurrency) -- offline, stubbed
extract_fn/quality backend with asyncio.sleep, no LLM, no network.

Brief 1 Step 3b's required cases: (1) N units at concurrency 4 take ~N/4 of
the sequential time; (2) output follows unit order even when later units
finish first; (3) one malformed unit doesn't fail the page; (4) a
RateLimitError in one unit releases the row with backoff and cancels its
siblings; (5) concurrency 1 matches the original output on a real fixture.
Plus: quality scoring runs under the same limit and still fails loudly, and
concurrency 1 keeps the original stop-at-first-failure call order.
"""
import asyncio
import json
import os
import time
import unittest

from config import ExtractionStrategy
from content.extraction_units import make_extraction_units_fn
from crawl.frontier import Frontier
from crawl.pipeline import RateLimitError, extract_worker
from decisions import DecisionError
from decisions.types import NoulAnswer

SEED = "https://x.test/page"
CONTENT = "page content"
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "docs_manim_community.rendered.html")
with open(FIXTURE, encoding="utf-8") as _f:  # read once, synchronously, at import
    FIXTURE_TEXT = _f.read()


def pair_json(unit: str) -> str:
    return json.dumps([{"instruction": f"Q:{unit}", "response": f"A:{unit}"}])


def units_of(units):
    async def units_fn(content):
        return list(units)
    return units_fn


async def recrawl_stub(frontier, content_queue, content, poll_interval=0.01):
    while True:
        row = await frontier.claim()
        if row is None:
            if frontier.quiescent.is_set():
                return
            await asyncio.sleep(poll_interval)
            continue
        await frontier.put_content(content_queue, (row.url, content))


async def score_one(url, content):
    return 1.0


class UnitConcurrencyCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frontier = Frontier(":memory:", max_retries=3)
        await self.frontier.open()
        await self.frontier.seed([(SEED, None)])
        self.cq = asyncio.Queue(maxsize=4)
        self.rq = asyncio.Queue(maxsize=4)
        self.tasks = []

    async def asyncTearDown(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.frontier.close()

    def spawn(self, extract_fn, units_fn, concurrency, quality_backend=None, content=CONTENT):
        self.tasks = [
            asyncio.create_task(extract_worker(
                self.frontier, self.cq, self.rq, score_one, extract_fn, 0.5, 0.5,
                extraction_units_fn=units_fn, poll_interval=0.01,
                quality_backend=quality_backend, quality_threshold=0.5,
                unit_concurrency=concurrency,
            )),
            asyncio.create_task(recrawl_stub(self.frontier, self.cq, content)),
        ]

    async def result(self, timeout=10.0):
        return await asyncio.wait_for(self.rq.get(), timeout=timeout)

    async def row(self):
        cur = await self.frontier._conn.execute(
            "SELECT status, not_before, last_error, retry_count FROM frontier WHERE url = ?", (SEED,)
        )
        return await cur.fetchone()


class TestRequiredCases(UnitConcurrencyCase):
    async def _timed_run(self, concurrency, n=8, delay=0.1):
        async def extract_fn(unit):
            await asyncio.sleep(delay)
            return pair_json(unit)

        self.spawn(extract_fn, units_of([f"u{i}" for i in range(n)]), concurrency)
        started = time.perf_counter()
        result = await self.result()
        return time.perf_counter() - started, result

    async def test_1_concurrency_4_takes_about_a_quarter_of_sequential_time(self):
        elapsed, result = await self._timed_run(concurrency=4)
        self.assertEqual(len(result.qa_pairs), 8)
        # 8 units x 0.1s at a limit of 4 = 2 waves = ~0.2s (sequential: 0.8s).
        self.assertLess(elapsed, 0.45)
        self.assertGreaterEqual(elapsed, 0.18)  # the semaphore really limits to 4, not 8

    async def test_1b_sequential_baseline_for_comparison(self):
        elapsed, result = await self._timed_run(concurrency=1)
        self.assertEqual(len(result.qa_pairs), 8)
        self.assertGreaterEqual(elapsed, 0.75)

    async def test_2_output_follows_unit_order_when_later_units_finish_first(self):
        units = [f"u{i}" for i in range(6)]
        finished = []

        async def extract_fn(unit):
            await asyncio.sleep(0.05 * (len(units) - int(unit[1:])))  # u5 fastest, u0 slowest
            finished.append(unit)
            return pair_json(unit)

        self.spawn(extract_fn, units_of(units), concurrency=6)
        result = await self.result()
        self.assertEqual(finished, list(reversed(units)))  # really did finish in reverse
        self.assertEqual([p["chunk_index"] for p in result.qa_pairs], list(range(6)))
        self.assertEqual([p["source_chunk"] for p in result.qa_pairs], units)
        self.assertEqual([p["question"] for p in result.qa_pairs], [f"Q:{u}" for u in units])

    async def test_3_one_malformed_unit_among_good_ones_page_still_succeeds(self):
        async def extract_fn(unit):
            await asyncio.sleep(0.01)
            return "not json" if unit == "u2" else pair_json(unit)

        self.spawn(extract_fn, units_of([f"u{i}" for i in range(5)]), concurrency=4)
        result = await self.result()
        self.assertEqual([p["chunk_index"] for p in result.qa_pairs], [0, 1, 3, 4])

    async def test_3b_all_units_malformed_fails_the_page(self):
        async def extract_fn(unit):
            return "not json"

        self.spawn(extract_fn, units_of(["a", "b", "c"]), concurrency=4)
        for _ in range(300):
            if (await self.row())["last_error"]:
                break
            await asyncio.sleep(0.01)
        self.assertIn("malformed_json", (await self.row())["last_error"])
        self.assertTrue(self.rq.empty())

    async def test_4_rate_limit_releases_row_with_backoff_and_cancels_siblings(self):
        started, finished, cancelled = set(), set(), set()
        rate_limited = asyncio.Event()

        async def extract_fn(unit):
            started.add(unit)
            try:
                if unit == "u1":
                    await asyncio.sleep(0.02)
                    rate_limited.set()
                    raise RateLimitError(retry_after=60)  # long: no retry within this test
                await asyncio.sleep(1.0)  # siblings still in flight when u1 fails
                finished.add(unit)
                return pair_json(unit)
            except asyncio.CancelledError:
                cancelled.add(unit)
                raise

        self.spawn(extract_fn, units_of([f"u{i}" for i in range(4)]), concurrency=4)
        await asyncio.wait_for(rate_limited.wait(), timeout=5)
        for _ in range(100):
            row = await self.row()
            if row["status"] == "queued" and row["not_before"]:
                break
            await asyncio.sleep(0.01)

        row = await self.row()
        self.assertEqual(row["status"], "queued")  # released, not failed, not in_progress
        self.assertIsNotNone(row["not_before"])  # backoff set via not_before -- no worker sleep
        self.assertEqual(row["last_error"], "rate_limited (extraction)")
        self.assertEqual(started, {"u0", "u1", "u2", "u3"})
        self.assertEqual(cancelled, {"u0", "u2", "u3"})  # every sibling cancelled...
        self.assertEqual(finished, set())  # ...before finishing
        self.assertTrue(self.rq.empty())  # no partial page written

    async def test_4b_rate_limit_wins_over_a_sibling_error(self):
        async def extract_fn(unit):
            if unit == "u0":
                raise ValueError("boom")
            if unit == "u1":
                raise RateLimitError(retry_after=60)
            await asyncio.sleep(1.0)
            return pair_json(unit)

        self.spawn(extract_fn, units_of(["u0", "u1", "u2"]), concurrency=3)
        for _ in range(200):
            if (await self.row())["last_error"]:
                break
            await asyncio.sleep(0.01)
        row = await self.row()
        self.assertEqual(row["last_error"], "rate_limited (extraction)")
        self.assertIsNotNone(row["not_before"])

    async def test_4c_other_exception_marks_failed_with_reason(self):
        async def extract_fn(unit):
            if unit == "u1":
                raise ValueError("boom")
            await asyncio.sleep(0.5)
            return pair_json(unit)

        self.spawn(extract_fn, units_of(["u0", "u1", "u2"]), concurrency=3)
        for _ in range(200):
            if (await self.row())["last_error"]:
                break
            await asyncio.sleep(0.01)
        self.assertEqual((await self.row())["last_error"], "extraction: ValueError: boom")

    async def test_5_concurrency_1_matches_concurrency_4_on_a_real_fixture(self):
        """Real fixture content through the real per_chunk unit selection;
        deterministic stub calls with randomized-ish latencies. The
        concurrency-1 path is also checked against the pre-change
        implementation by the existing tests/test_extract_worker.py suite,
        which runs unmodified at the default of 1."""
        content = FIXTURE_TEXT
        units_fn = make_extraction_units_fn(ExtractionStrategy.PER_CHUNK)
        n_units = len(await units_fn(content))
        self.assertGreater(n_units, 4)  # a real multi-unit page, not a trivial one

        async def extract_fn(unit):
            await asyncio.sleep((len(unit) % 7) * 0.002)
            return json.dumps([
                {"instruction": f"Q{len(unit)}", "response": unit[:40]},
                {"instruction": f"Q{len(unit)}b", "response": unit[-40:]},
            ])

        outputs = []
        for concurrency in (1, 4):
            await self.asyncTearDown()
            await self.asyncSetUp()
            self.spawn(extract_fn, units_fn, concurrency, content=content)
            result = await self.result()
            outputs.append([{k: v for k, v in p.items() if k not in ("timestamp", "crawl_date")}
                            for p in result.qa_pairs])
        self.assertEqual(len(outputs[0]), 2 * n_units)
        self.assertEqual(outputs[0], outputs[1])


class TestQualityScoringUnderTheLimit(UnitConcurrencyCase):
    async def test_quality_calls_run_concurrently_and_respect_the_limit(self):
        in_flight = {"now": 0, "max": 0}

        async def extract_fn(unit):
            return json.dumps([{"instruction": f"Q{unit}{i}", "response": "A"} for i in range(4)])

        class SlowQuality:
            async def noul(self, text, question):
                in_flight["now"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["now"])
                await asyncio.sleep(0.05)
                in_flight["now"] -= 1
                return NoulAnswer(value=0.9, method="traditional")

        self.spawn(extract_fn, units_of(["a", "b", "c"]), concurrency=3, quality_backend=SlowQuality())
        started = time.perf_counter()
        result = await self.result()
        elapsed = time.perf_counter() - started
        self.assertEqual(len(result.qa_pairs), 12)
        self.assertEqual(in_flight["max"], 3)  # concurrent, and capped at the limit (no deadlock)
        self.assertLess(elapsed, 0.45)  # 12 calls x 0.05s sequentially would be 0.6s

    async def test_quality_failure_still_fails_the_page_loudly(self):
        async def extract_fn(unit):
            return pair_json(unit)

        class FailingQuality:
            async def noul(self, text, question):
                raise DecisionError("quality backend down")

        self.spawn(extract_fn, units_of(["a", "b"]), concurrency=2, quality_backend=FailingQuality())
        for _ in range(200):
            if (await self.row())["last_error"]:
                break
            await asyncio.sleep(0.01)
        self.assertIn("DecisionError", (await self.row())["last_error"])
        self.assertTrue(self.rq.empty())  # never a silent keep


class TestConcurrencyOneIsStrictlySequential(UnitConcurrencyCase):
    async def test_failure_stops_before_later_units_are_called(self):
        called, in_flight = [], {"now": 0, "max": 0}

        async def extract_fn(unit):
            called.append(unit)
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            await asyncio.sleep(0.01)
            in_flight["now"] -= 1
            if unit == "u1":
                raise ValueError("boom")
            return pair_json(unit)

        self.spawn(extract_fn, units_of(["u0", "u1", "u2", "u3"]), concurrency=1)
        for _ in range(200):
            if (await self.row())["last_error"]:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(called[:2], ["u0", "u1"])
        self.assertNotIn("u2", called[:3])  # u2 not called in the failing attempt
        self.assertEqual(in_flight["max"], 1)

    async def test_invalid_concurrency_is_rejected(self):
        with self.assertRaises(ValueError):
            await extract_worker(self.frontier, self.cq, self.rq, score_one, None, 0.5, 0.5,
                                 unit_concurrency=0)


if __name__ == "__main__":
    unittest.main()
