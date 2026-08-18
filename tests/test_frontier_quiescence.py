"""Proves the global-quiescence termination design (not a per-stage
sentinel cascade) survives the scenario that breaks a cascade: a slow
extract worker causes the crawl stage to run out of claimable work and go
idle multiple times mid-run, while more work is still coming (each idle
period ends only once a promotion from the extract worker unblocks the
next wave).

Graph (children discovered only once their parent is actually crawled):

    A (seed, depth 0)
    +-- B (depth 1, exempt -- auto-queued, no score needed)
    |   +-- D (depth 2, gated -- needs B's score to promote)
    |   |   +-- H (depth 3, gated -- needs D's score to promote)
    |   +-- E (depth 2, gated, leaf)
    +-- C (depth 1, exempt -- auto-queued, no score needed)
        +-- F (depth 2, gated, leaf)
        +-- G (depth 2, gated, leaf)

Crawl fetches are ~instant; extraction is deliberately slow (50ms) relative
to it, so the crawl worker exhausts `queued` and blocks on claim() while
waiting for the extract worker to catch up and promote the next wave. A
per-stage sentinel cascade would see "crawl idle, nothing queued" after the
first wave and shut the crawl stage down -- ending the run with D/E/F/G/H
never discovered. Global quiescence must not do that.
"""
import asyncio
import unittest

from frontier import Frontier

HOST = "https://x.test"
CHILDREN = {
    f"{HOST}/a": [f"{HOST}/b", f"{HOST}/c"],
    f"{HOST}/b": [f"{HOST}/d", f"{HOST}/e"],
    f"{HOST}/c": [f"{HOST}/f", f"{HOST}/g"],
    f"{HOST}/d": [f"{HOST}/h"],
    f"{HOST}/e": [],
    f"{HOST}/f": [],
    f"{HOST}/g": [],
    f"{HOST}/h": [],
}
ALL_URLS = list(CHILDREN)

EXTRACT_DELAY = 0.05
CRAWL_DELAY = 0.001
POLL_INTERVAL = 0.005


class TestFrontierQuiescence(unittest.IsolatedAsyncioTestCase):
    async def test_quiescence_survives_multiple_crawl_idle_periods(self):
        frontier = Frontier(":memory:", follow_gate_exempt_depth=0)
        await frontier.open()

        content_queue: asyncio.Queue = asyncio.Queue(maxsize=4)
        results_queue: asyncio.Queue = asyncio.Queue(maxsize=4)

        idle_transitions = 0
        was_idle = False
        idle_lock = asyncio.Lock()

        async def crawl_worker():
            nonlocal idle_transitions, was_idle
            while True:
                row = await frontier.claim()
                if row is None:
                    if frontier.quiescent.is_set():
                        return
                    async with idle_lock:
                        if not was_idle:
                            idle_transitions += 1
                            was_idle = True
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                async with idle_lock:
                    was_idle = False
                await asyncio.sleep(CRAWL_DELAY)  # simulated fetch
                children = [(u, None) for u in CHILDREN[row.url]]
                await frontier.insert_children(row.url, row.depth, children)
                await frontier.put_content(content_queue, row.url)

        async def extract_worker():
            while True:
                try:
                    url = await asyncio.wait_for(content_queue.get(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    if frontier.quiescent.is_set():
                        return
                    continue
                await asyncio.sleep(EXTRACT_DELAY)  # simulated slow scoring/extraction
                await frontier.score_and_resolve_children(url, score=1.0, promote=True)
                await frontier.put_results(results_queue, url)
                await frontier.content_done(content_queue)

        async def writer():
            while True:
                try:
                    url = await asyncio.wait_for(results_queue.get(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    if frontier.quiescent.is_set():
                        return
                    continue
                await frontier.mark_written(url)
                await frontier.results_done(results_queue)

        await frontier.seed([(f"{HOST}/a", None)])

        tasks = [
            asyncio.create_task(crawl_worker()),
            asyncio.create_task(extract_worker()),
            asyncio.create_task(writer()),
        ]

        await asyncio.wait_for(frontier.quiescent.wait(), timeout=10)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("done", 0), len(ALL_URLS), counts)
        self.assertNotIn("queued", counts)
        self.assertNotIn("pending_score", counts)
        self.assertNotIn("in_progress", counts)

        # The actual regression check: a per-stage cascade shuts the crawl
        # stage down on the FIRST idle period. This graph requires at least
        # 3 promotions (B's, C's, D's) after the initial wave, so at least
        # 2-3 distinct idle-then-resume cycles must have happened for every
        # node to have been discovered and reached 'done'.
        self.assertGreaterEqual(
            idle_transitions, 2,
            f"expected the crawl stage to idle at least twice waiting on the slow "
            f"extract worker, only observed {idle_transitions} -- graph/timing may "
            f"not be exercising the regression this test targets",
        )

        await frontier.close()


if __name__ == "__main__":
    unittest.main()
