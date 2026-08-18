"""crawl_worker failure-mode tests -- one deterministic stub per failure
mode, each asserting the resulting frontier row status. Offline: no
network, plain async stub functions as fetch_fn.

Note: orphan resolution (pending_score children of a permanently-failed
parent) can't be exercised via a crawl-only failure here -- children are
only discovered *after* a successful fetch (insert_children runs in the
success path), so a row whose fetch never once succeeds never has any
children to orphan. The real path to that scenario is crash-recovery
exhausting retries on a row that discovered children before the crash --
see tests/test_frontier.py's direct orphan-resolution test instead, which
tests frontier.py's mechanism in isolation rather than contriving a
crawl_worker setup to reach it indirectly.
"""
import asyncio
import time
import unittest

from crawl.frontier import Frontier
from crawl.pipeline import crawl_worker, FetchTimeout, FetchHTTPError
from crawl.politeness import HostPoliteness


SEED = "https://x.test/a"


async def run_to_quiescence(task, frontier, timeout=5):
    await asyncio.wait_for(frontier.quiescent.wait(), timeout=timeout)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestCrawlWorkerFailureModes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frontier = Frontier(":memory:")
        await self.frontier.open()
        await self.frontier.seed([(SEED, None)])
        self.content_queue = asyncio.Queue(maxsize=4)

    async def asyncTearDown(self):
        await self.frontier.close()

    async def test_fetch_timeout_retries_then_terminally_fails(self):
        attempts = []

        async def always_timeout(url):
            attempts.append(url)
            raise FetchTimeout("simulated timeout")

        task = asyncio.create_task(
            crawl_worker(self.frontier, always_timeout, self.content_queue, lambda u: True, poll_interval=0.01)
        )
        await run_to_quiescence(task, self.frontier)

        self.assertEqual(len(attempts), 3)  # default MAX_RETRIES=3 attempts total
        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("failed"), 1)
        self.assertNotIn("queued", counts)
        self.assertNotIn("in_progress", counts)

    async def test_non_200_marks_failed_after_retries(self):
        async def always_404(url):
            raise FetchHTTPError(404)

        task = asyncio.create_task(
            crawl_worker(self.frontier, always_404, self.content_queue, lambda u: True, poll_interval=0.01)
        )
        await run_to_quiescence(task, self.frontier)

        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("failed"), 1)

    async def test_transient_failure_then_success_does_not_fail_permanently(self):
        calls = {"n": 0}

        async def fails_once_then_succeeds(url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FetchTimeout("first attempt times out")
            return "<html>ok</html>", []

        async def drain():
            while True:
                url, _content = await self.content_queue.get()
                await self.frontier.mark_written(url)
                await self.frontier.content_done(self.content_queue)

        drain_task = asyncio.create_task(drain())
        task = asyncio.create_task(
            crawl_worker(self.frontier, fails_once_then_succeeds, self.content_queue, lambda u: True, poll_interval=0.01)
        )
        await run_to_quiescence(task, self.frontier)
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

        self.assertEqual(calls["n"], 2)
        counts = await self.frontier.counts_by_status()
        self.assertNotIn("failed", counts)
        self.assertEqual(counts.get("done"), 1)

    async def test_robots_disallowed_skips_fetch_entirely_no_retry(self):
        fetch_calls = []

        async def fetch_fn(url):
            fetch_calls.append(url)
            return "<html></html>", []

        class AlwaysDisallowRobots:
            async def get_policy(self, host):
                class P:
                    def is_allowed(self_, url):
                        return False
                return P()

        task = asyncio.create_task(
            crawl_worker(
                self.frontier, fetch_fn, self.content_queue, lambda u: True,
                robots_cache=AlwaysDisallowRobots(), poll_interval=0.01,
            )
        )
        await run_to_quiescence(task, self.frontier)

        self.assertEqual(fetch_calls, [])  # never actually fetched
        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("failed"), 1)  # permanent, no retry attempts

    async def test_successful_fetch_discovers_children_within_scope_only(self):
        async def fetch_fn(url):
            return "<html>content</html>", [
                "https://x.test/in-scope",
                "https://other.test/off-scope",
            ]

        async def drain():
            # Simulates the extract+write stages just enough to resolve
            # each row to a terminal status -- content_done() alone only
            # closes the content-queue obligation, not the row's claim.
            while True:
                url, _content = await self.content_queue.get()
                await self.frontier.mark_written(url)
                await self.frontier.content_done(self.content_queue)

        drain_task = asyncio.create_task(drain())
        task = asyncio.create_task(
            crawl_worker(
                self.frontier, fetch_fn, self.content_queue,
                scope_check=lambda u: u.startswith("https://x.test"),
                poll_interval=0.01,
            )
        )
        await run_to_quiescence(task, self.frontier)
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

        cur_counts = await self.frontier.counts_by_status()
        self.assertEqual(cur_counts.get("done"), 2)  # seed + its in-scope child
        urls = await self.frontier.get_all_urls()
        self.assertIn("https://x.test/in-scope", urls)
        self.assertNotIn("https://other.test/off-scope", urls)  # never inserted at all

    async def test_politeness_spaces_out_successive_fetches_to_the_same_host(self):
        # Real regression the chrome_strip wiring gap (LESSONS_LEARNED.md
        # #28) established a pattern for: prove the dependency is actually
        # invoked through the real crawl_worker call site, not just that
        # HostPoliteness itself works in isolation (tests/test_politeness.py).
        frontier = Frontier(":memory:")
        await frontier.open()
        await frontier.seed([("https://x.test/a", None), ("https://x.test/b", None)])
        content_queue = asyncio.Queue(maxsize=4)
        fetch_times = []

        async def fetch_fn(url):
            fetch_times.append(time.monotonic())
            return "<html></html>", []

        async def drain():
            while True:
                url, _content = await content_queue.get()
                await frontier.mark_written(url)
                await frontier.content_done(content_queue)

        drain_task = asyncio.create_task(drain())
        delay = 0.15
        politeness = HostPoliteness(max_concurrent_per_host=5, default_delay_seconds=delay)
        task = asyncio.create_task(
            crawl_worker(frontier, fetch_fn, content_queue, lambda u: True, politeness=politeness, poll_interval=0.01)
        )
        await run_to_quiescence(task, frontier)
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass
        await frontier.close()

        self.assertEqual(len(fetch_times), 2)
        self.assertGreaterEqual(fetch_times[1] - fetch_times[0], delay * 0.9)


if __name__ == "__main__":
    unittest.main()
