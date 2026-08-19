"""Regression test for a real hang found resuming a completed FastAPI
crawl (see LESSONS_LEARNED.md #44): the quiescence check was only ever
reachable from a decrement to `_in_flight` (a successful claim(), or
put_content/put_results resolving) -- every one of those requires
something to still be in flight in *this* process. A process that
starts in an already-final state has no such decrement to ever trigger
it from, so `quiescent` never got set and every worker looped
claim()->None forever.

Three states can be "already final" the moment a process starts, before
any worker runs -- each exercised here against a real on-disk db file
(":memory:" can't simulate "a prior process already wrote this state
and exited", since a fresh :memory: connection is always a blank
database). recover_crashed() is the real startup call site
(crawl/frontier.py -- main.py calls it right after frontier.open(),
before any worker task is created), so these test through it, not a
bare internal method call.
"""
import asyncio
import os
import tempfile
import unittest

from crawl.frontier import Frontier

HOST = "https://x.test"


class TestStartupQuiescence(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "frontier.db")

    async def asyncTearDown(self):
        self._tmpdir.cleanup()

    async def test_empty_frontier_quiescent_immediately(self):
        """Nothing ever seeded -- e.g. a frontier.db created but the
        process died before seed() ran."""
        frontier = Frontier(self.db_path)
        await frontier.open()
        await frontier.recover_crashed()
        self.assertTrue(frontier.quiescent.is_set())
        await frontier.close()

    async def test_all_terminal_frontier_quiescent_on_resume(self):
        """Every row already done/failed, nothing in_progress -- a
        completed run's db reopened by a fresh process."""
        setup = Frontier(self.db_path)
        await setup.open()
        await setup.seed([(f"{HOST}/a", None), (f"{HOST}/b", None)])
        row_a = await setup.claim()
        row_b = await setup.claim()
        await setup.mark_written(row_a.url)
        await setup.mark_permanently_failed(row_b.url, "simulated")
        await setup.close()

        frontier = Frontier(self.db_path)
        await frontier.open()
        recovery = await frontier.recover_crashed()
        self.assertEqual(recovery, {"requeued": 0, "failed": 0})  # nothing was in_progress
        self.assertTrue(frontier.quiescent.is_set())
        await frontier.close()

    async def test_cap_met_frontier_quiescent_on_resume_with_queued_rows_left(self):
        """max_pages already reached, nothing in_progress, but rows
        remain 'queued' by design (ROADMAP #28's overshoot behavior) --
        this is exactly the state the real FastAPI resume hung in."""
        setup = Frontier(self.db_path, max_pages=1)
        await setup.open()
        await setup.seed([(f"{HOST}/a", None), (f"{HOST}/b", None)])
        row_a = await setup.claim()
        await setup.mark_written(row_a.url)  # done count now 1, meets the cap
        await setup.close()
        # row_b is still 'queued' -- the cap was reached before it was claimed

        frontier = Frontier(self.db_path, max_pages=1)
        await frontier.open()
        await frontier.recover_crashed()
        self.assertTrue(frontier.quiescent.is_set())
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 1)  # left live, not drained
        await frontier.close()

    async def test_workers_actually_exit_on_an_already_quiescent_resume(self):
        """Not just that the Event gets set -- that a real crawl_worker
        loop (claim() -> None -> check quiescent -> return) actually
        terminates instead of hanging, through the same loop shape
        crawl_worker() uses."""
        setup = Frontier(self.db_path, max_pages=1)
        await setup.open()
        await setup.seed([(f"{HOST}/a", None)])
        row_a = await setup.claim()
        await setup.mark_written(row_a.url)
        await setup.close()

        frontier = Frontier(self.db_path, max_pages=1)
        await frontier.open()
        await frontier.recover_crashed()

        async def worker_loop():
            while True:
                row = await frontier.claim()
                if row is None:
                    if frontier.quiescent.is_set():
                        return
                    await asyncio.sleep(0.01)
                    continue

        await asyncio.wait_for(worker_loop(), timeout=2)  # would hang forever pre-fix
        await frontier.close()

    async def test_seed_after_recover_crashed_on_fresh_db_unlatches_quiescence(self):
        """The real main.py startup sequence -- recover_crashed() then
        seed() in the same process, on a frontier.db that has never been
        seeded before. recover_crashed() correctly (if misleadingly)
        concludes an empty frontier is quiescent (see
        test_empty_frontier_quiescent_immediately); seed() then inserts
        the first real work. Found live running the first real crawl
        attempt: without this, quiescent stays latched from the earlier
        check and every crawl_worker exits before claiming anything --
        `main.py` prints `Done. {'queued': N}` with zero pages ever
        fetched, no error, no hang. Same worker-loop shape as
        test_workers_actually_exit_on_an_already_quiescent_resume, but
        proving the opposite: this one must actually do the claimed work,
        not just exit cleanly."""
        frontier = Frontier(self.db_path)
        await frontier.open()
        await frontier.recover_crashed()
        self.assertTrue(frontier.quiescent.is_set())  # the misleading-but-correct interim state

        await frontier.seed([(f"{HOST}/a", None), (f"{HOST}/b", None)])
        self.assertFalse(frontier.quiescent.is_set(), "seed() must un-latch quiescence when it adds real new work")

        claimed = []

        async def worker_loop():
            while True:
                row = await frontier.claim()
                if row is None:
                    if frontier.quiescent.is_set():
                        return
                    await asyncio.sleep(0.01)
                    continue
                claimed.append(row.url)
                await frontier.mark_written(row.url)

        await asyncio.wait_for(worker_loop(), timeout=2)
        self.assertEqual(sorted(claimed), [f"{HOST}/a", f"{HOST}/b"])
        await frontier.close()

    async def test_reseeding_a_completed_run_with_the_same_urls_does_not_unlatch(self):
        """The other real main.py sequence: resuming a run whose db
        already holds every original seed url in a terminal state, where
        main.py calls seed() again unconditionally with that same list.
        INSERT OR IGNORE makes this a no-op -- must NOT un-latch
        quiescence, or this regresses straight back into LESSONS_LEARNED
        #44's hang (nothing left to ever decrement in_flight and
        re-trigger the check)."""
        seeds = [(f"{HOST}/a", None), (f"{HOST}/b", None)]
        setup = Frontier(self.db_path)
        await setup.open()
        await setup.seed(seeds)
        row_a = await setup.claim()
        row_b = await setup.claim()
        await setup.mark_written(row_a.url)
        await setup.mark_permanently_failed(row_b.url, "simulated")
        await setup.close()

        frontier = Frontier(self.db_path)
        await frontier.open()
        await frontier.recover_crashed()
        self.assertTrue(frontier.quiescent.is_set())

        await frontier.seed(seeds)  # same urls, already terminal -- no new rows
        self.assertTrue(frontier.quiescent.is_set(), "re-seeding with no genuinely new urls must not un-latch quiescence")

        async def worker_loop():
            while True:
                row = await frontier.claim()
                if row is None:
                    if frontier.quiescent.is_set():
                        return
                    await asyncio.sleep(0.01)
                    continue

        await asyncio.wait_for(worker_loop(), timeout=2)  # would hang if incorrectly un-latched
        await frontier.close()


if __name__ == "__main__":
    unittest.main()
