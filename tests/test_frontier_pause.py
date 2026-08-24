"""Frontier.set_paused()/is_paused(): pause must stop new claims without
ever making quiescence look like completion. This is explicitly the third
shape of the same bug class (LESSONS_LEARNED.md #44, and the cancellation
work before this): "in_flight==0 and nothing claimable" is quiescence's
own trigger condition, and a paused run can genuinely reach in_flight==0
(every in-flight page finishes naturally) while queued rows sit untouched
-- if quiescence didn't know to ignore that, a paused run would look
exactly like a finished one and shut itself down.
"""
import asyncio
import unittest

from crawl.frontier import Frontier

HOST = "https://x.test"


class TestPauseDoesNotFalselyQuiesce(unittest.IsolatedAsyncioTestCase):
    async def test_pause_with_queued_rows_and_zero_in_flight_stays_alive(self):
        """The exact scenario asked for: pause with queued rows and zero
        in-flight, and the run must not terminate."""
        frontier = Frontier(":memory:")
        await frontier.open()
        await frontier.seed([(f"{HOST}/a", None), (f"{HOST}/b", None)])
        # in_flight is 0 here -- nothing has ever been claimed.
        self.assertEqual(frontier._in_flight, 0)

        await frontier.set_paused(True)

        # claim() must refuse (paused), not because nothing is queued.
        row = await frontier.claim()
        self.assertIsNone(row)
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 2)  # untouched, still genuinely queued

        # Force a real quiescence re-evaluation the way a real completion
        # path does (recover_crashed() unconditionally calls it at
        # startup -- the exact mechanism entry #44 required for this
        # reason: quiescence must be reachable even from a state with no
        # decrement of its own).
        await frontier.recover_crashed()

        self.assertFalse(frontier.quiescent.is_set(), "paused run with queued rows must not quiesce")
        await frontier.close()

    async def test_pause_after_in_flight_work_drains_to_zero_still_stays_alive(self):
        """A stronger version: something WAS in flight, finishes normally
        while paused, driving in_flight to exactly 0 through the real
        decrement path (_locked_leave_in_progress) -- the scenario where a
        naive quiescence check (in_flight==0, nothing else considered)
        would be most tempted to fire."""
        frontier = Frontier(":memory:")
        await frontier.open()
        await frontier.seed([(f"{HOST}/a", None), (f"{HOST}/b", None), (f"{HOST}/c", None)])

        claimed = await frontier.claim()  # a (or whichever) -- in_flight=1
        self.assertIsNotNone(claimed)

        await frontier.set_paused(True)

        # The in-flight page finishes naturally (pause never aborts
        # in-flight work -- see set_paused's docstring), driving
        # in_flight back to 0 through the real decrement path.
        await frontier.mark_written(claimed.url)
        self.assertEqual(frontier._in_flight, 0)

        self.assertFalse(frontier.quiescent.is_set(), "must not quiesce the instant in_flight hits 0 while paused")

        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 2)  # the other two, never claimed because paused

        await frontier.close()

    async def test_unpause_lets_the_run_proceed_and_finish_normally(self):
        """Resuming must not just stay non-quiescent forever -- real
        workers, actually polling claim(), must pick back up and the run
        must reach genuine completion once unpaused."""
        frontier = Frontier(":memory:")
        await frontier.open()
        urls = [f"{HOST}/a", f"{HOST}/b", f"{HOST}/c"]
        await frontier.seed([(u, None) for u in urls])
        await frontier.set_paused(True)

        async def worker():
            while True:
                if frontier.quiescent.is_set():
                    return
                row = await frontier.claim()
                if row is None:
                    if frontier.quiescent.is_set():
                        return
                    await asyncio.sleep(0.005)
                    continue
                await frontier.mark_written(row.url)

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.05)  # plenty of time to (wrongly) quiesce if paused isn't respected
        self.assertFalse(frontier.quiescent.is_set())
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 3)  # nothing claimed at all while paused

        await frontier.set_paused(False)
        await asyncio.wait_for(frontier.quiescent.wait(), timeout=5)
        await asyncio.wait_for(task, timeout=5)

        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("done"), 3)
        await frontier.close()

    async def test_is_paused_reflects_db_state(self):
        frontier = Frontier(":memory:")
        await frontier.open()
        self.assertFalse(await frontier.is_paused())
        await frontier.set_paused(True)
        self.assertTrue(await frontier.is_paused())
        await frontier.set_paused(False)
        self.assertFalse(await frontier.is_paused())
        await frontier.close()


if __name__ == "__main__":
    unittest.main()
