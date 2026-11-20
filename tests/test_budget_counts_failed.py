"""Failed pages count toward max_pages when config.MAX_PAGES_COUNTS_FAILED is
on (crawl/frontier.py count_failed_toward_budget; ROADMAP.md #54; Brief 1 3c
item B) -- offline: real Frontier + real crawl_worker/map_worker, stub
fetch_fn, no network.

Covers: a run whose fetches all fail stops at its budget (and quiesces --
claim() and the quiescence check use one shared predicate); robots.txt
disallows never consume budget; mixed done+failed totals; failed rows in a
resumed frontier.db count like done rows; flag off = original behaviour.
"""
import asyncio
import os
import tempfile
import unittest

from crawl.frontier import ROBOTS_DISALLOWED_ERROR, Frontier
from crawl.pipeline import FetchHTTPError, crawl_worker, map_worker

SEEDS = [(f"https://x.test/p{i}", None) for i in range(10)]
MAX_RETRIES = 3


async def run_to_quiescence(frontier, fetch_fn, workers=2, timeout=10):
    cq = asyncio.Queue(maxsize=4)
    tasks = [asyncio.create_task(crawl_worker(frontier, fetch_fn, cq, lambda u: True, poll_interval=0.005))
             for _ in range(workers)]
    tasks.append(asyncio.create_task(map_worker(frontier, cq, poll_interval=0.005)))
    try:
        await asyncio.wait_for(frontier.quiescent.wait(), timeout=timeout)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class BudgetCase(unittest.IsolatedAsyncioTestCase):
    async def make(self, count_failed, max_pages=4, path=":memory:", seeds=SEEDS):
        f = Frontier(path, max_pages=max_pages, max_retries=MAX_RETRIES,
                     count_failed_toward_budget=count_failed)
        await f.open()
        await f.recover_crashed()
        await f.seed(seeds)
        self.addAsyncCleanup(f.close)
        return f


class TestAllFetchesFail(BudgetCase):
    async def _run(self, count_failed):
        frontier = await self.make(count_failed)
        fetches = []

        async def failing_fetch(url):
            fetches.append(url)
            raise FetchHTTPError(500)

        await run_to_quiescence(frontier, failing_fetch)
        return frontier, fetches

    async def test_flag_on_stops_at_the_budget(self):
        frontier, fetches = await self._run(count_failed=True)
        counts = await frontier.counts_by_status()
        # Budget 4: claims stop once 4 rows are terminally failed. Up to one
        # extra row per worker may already be in flight (#28 overshoot).
        self.assertGreaterEqual(counts.get("failed", 0), 4)
        self.assertLessEqual(counts.get("failed", 0), 4 + 2)
        self.assertGreater(counts.get("queued", 0), 0)  # untouched pages left for a later run
        self.assertLessEqual(len(fetches), (4 + 2) * MAX_RETRIES)  # bounded, not 10 x 3

    async def test_flag_off_is_the_original_unbounded_behaviour(self):
        frontier, fetches = await self._run(count_failed=False)
        counts = await frontier.counts_by_status()
        self.assertEqual(counts, {"failed": 10})  # every page tried, budget never reached
        self.assertEqual(len(fetches), 10 * MAX_RETRIES)


class TestWhatCounts(BudgetCase):
    async def test_robots_disallowed_rows_never_consume_budget(self):
        frontier = await self.make(count_failed=True, max_pages=2, seeds=SEEDS[:5])
        for _ in range(3):  # 3 robots disallows: budget of 2 must still have room
            row = await frontier.claim()
            await frontier.mark_permanently_failed(row.url, ROBOTS_DISALLOWED_ERROR)
        self.assertIsNotNone(await frontier.claim())

    async def test_genuine_failures_do_consume_budget(self):
        frontier = await self.make(count_failed=True, max_pages=2, seeds=SEEDS[:5])
        for _ in range(2):
            row = await frontier.claim()
            await frontier.mark_permanently_failed(row.url, "browser_driver_dead: x")
        self.assertIsNone(await frontier.claim())

    async def test_retry_exhausted_extraction_failure_counts(self):
        frontier = await self.make(count_failed=True, max_pages=1, seeds=SEEDS[:3])
        row = await frontier.claim()
        url = row.url
        for attempt in range(MAX_RETRIES):
            if attempt:
                row = await frontier.claim()
                self.assertEqual(row.url, url)  # same page retried, not a new one
            await frontier.mark_extract_outcome(url, "failed", "extraction: ValueError: boom")
        self.assertIsNone(await frontier.claim())  # budget of 1 used by that terminal failure

    async def test_flag_off_failures_do_not_count(self):
        frontier = await self.make(count_failed=False, max_pages=2, seeds=SEEDS[:5])
        for _ in range(3):
            row = await frontier.claim()
            await frontier.mark_permanently_failed(row.url, "browser_driver_dead: x")
        self.assertIsNotNone(await frontier.claim())

    async def test_mixed_done_and_failed_reach_the_budget_together(self):
        frontier = await self.make(count_failed=True, max_pages=4)
        ok = {url for url, _ in SEEDS[:2]}

        async def fetch(url):
            if url in ok:
                return "content", []
            raise FetchHTTPError(503)

        await run_to_quiescence(frontier, fetch)
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("mapped"), 2)
        budget_used = counts.get("mapped", 0) + counts.get("failed", 0)
        self.assertGreaterEqual(budget_used, 4)
        self.assertLessEqual(budget_used, 4 + 2)


class TestResume(BudgetCase):
    async def test_failed_rows_in_a_resumed_db_count_like_done_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "frontier.db")
            first = Frontier(path, max_pages=None, max_retries=MAX_RETRIES)
            await first.open()
            await first.recover_crashed()
            await first.seed(SEEDS[:6])
            for _ in range(2):
                row = await first.claim()
                await first.mark_permanently_failed(row.url, "http_500")
            await first.close()

            resumed = await self.make(count_failed=True, max_pages=3, path=path, seeds=SEEDS[:6])
            row = await resumed.claim()  # 2 earlier failures + this one = budget of 3
            self.assertIsNotNone(row)
            await resumed.mark_permanently_failed(row.url, "http_500")
            self.assertIsNone(await resumed.claim())

            off = Frontier(path, max_pages=3, max_retries=MAX_RETRIES)
            await off.open()
            self.assertIsNotNone(await off.claim())  # flag off: those failures don't count
            await off.close()


if __name__ == "__main__":
    unittest.main()
