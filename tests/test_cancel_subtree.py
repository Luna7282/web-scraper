"""cancel() / requeue_for_extraction(): the new 'cancelled' status must not
break quiescence -- the exact class of bug this project has hit twice
before (see CLAUDE.md), both from a terminal state nothing moves out of.
Also covers the in-flight decision documented on Frontier.cancel(): a
node claimed before cancellation finishes naturally, but its children are
blocked from ever entering the frontier.

Graph (same shape as test_frontier_quiescence.py's, reused deliberately):

    A (seed)
    +-- B
    |   +-- D
    |   +-- E
    +-- C
        +-- F
        +-- G

Note: siblings inserted in the same insert_children() call share one
created_at timestamp, so claim()'s "ORDER BY created_at LIMIT 1" does not
guarantee which of two same-batch siblings comes back first -- tests below
never assert an exact claim order for siblings, only claim-drain-then-
look-up-by-url, matching how the rest of this suite (e.g.
test_frontier_quiescence.py) already stays order-agnostic.
"""
import asyncio
import unittest

from crawl.frontier import Frontier

HOST = "https://x.test"
CHILDREN = {
    f"{HOST}/a": [f"{HOST}/b", f"{HOST}/c"],
    f"{HOST}/b": [f"{HOST}/d", f"{HOST}/e"],
    f"{HOST}/c": [f"{HOST}/f", f"{HOST}/g"],
    f"{HOST}/d": [],
    f"{HOST}/e": [],
    f"{HOST}/f": [],
    f"{HOST}/g": [],
}


class TestCancelSubtreeBasics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frontier = Frontier(":memory:", follow_gate_exempt_depth=0)
        await self.frontier.open()
        await self.frontier.seed([(f"{HOST}/a", None)])

    async def asyncTearDown(self):
        await self.frontier.close()

    async def _claim_and_expand(self):
        """Claims whichever queued row comes back next, inserts its
        children, returns the row -- never assumes sibling claim order."""
        row = await self.frontier.claim()
        await self.frontier.insert_children(row.url, row.depth, [(c, None) for c in CHILDREN[row.url]])
        return row

    async def test_cancel_by_url_cancels_queued_subtree_leaves_terminal_rows_alone(self):
        a = await self._claim_and_expand()
        self.assertEqual(a.url, f"{HOST}/a")
        await self.frontier.mark_written(a.url)  # terminal -- must survive untouched
        await self._claim_and_expand()  # b
        await self._claim_and_expand()  # c

        cancelled = await self.frontier.cancel(url=f"{HOST}/b")
        self.assertEqual(set(cancelled), {f"{HOST}/b", f"{HOST}/d", f"{HOST}/e"})

        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("cancelled"), 3)
        self.assertEqual(counts.get("done"), 1)  # a untouched
        self.assertEqual(counts.get("in_progress"), 1)  # c, still claimed -- untouched
        self.assertEqual(counts.get("pending_score"), 2)  # c's children (f, g) remain live

    async def test_cancel_by_prefix(self):
        await self._claim_and_expand()  # a
        await self._claim_and_expand()  # one of b/c
        await self._claim_and_expand()  # the other of b/c

        cancelled = await self.frontier.cancel(prefix=f"{HOST}/b")
        self.assertIn(f"{HOST}/b", cancelled)

    async def test_recancelling_already_cancelled_subtree_is_a_noop(self):
        await self._claim_and_expand()  # a
        first = await self.frontier.cancel(url=f"{HOST}/b")
        self.assertTrue(first)
        second = await self.frontier.cancel(url=f"{HOST}/b")
        self.assertEqual(second, [])

    async def test_cancel_requires_exactly_one_of_url_or_prefix(self):
        with self.assertRaises(ValueError):
            await self.frontier.cancel()
        with self.assertRaises(ValueError):
            await self.frontier.cancel(url=f"{HOST}/b", prefix=f"{HOST}/b")


class TestCancelMidFlightBlocksChildren(unittest.IsolatedAsyncioTestCase):
    """The in-flight decision: a claimed-but-not-yet-finished node keeps
    running (no abort mechanism exists), but cancel() marks it cancelled
    immediately, so when it finishes and tries to insert its children,
    _locked_insert_children sees 'cancelled' and refuses -- the subtree
    can't regrow through the one node cancel() couldn't reach in time."""

    async def test_children_of_a_cancelled_in_progress_node_are_never_inserted(self):
        frontier = Frontier(":memory:", follow_gate_exempt_depth=0)
        await frontier.open()
        await frontier.seed([(f"{HOST}/a", None)])

        row = await frontier.claim()  # a is now in_progress -- simulating a fetch in flight
        self.assertEqual(row.url, f"{HOST}/a")

        cancelled = await frontier.cancel(url=f"{HOST}/a")
        self.assertEqual(cancelled, [f"{HOST}/a"])
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("cancelled"), 1)

        # The "fetch" now completes and the worker calls insert_children,
        # exactly as crawl_worker does after a successful fetch.
        await frontier.insert_children(row.url, row.depth, [(f"{HOST}/b", None), (f"{HOST}/c", None)])

        urls = await frontier.get_all_urls()
        self.assertEqual(urls, {f"{HOST}/a"})  # b, c never entered the frontier

        # The worker still finishes its own claim normally (mark_written
        # etc overwrite the DB's 'cancelled' with the real outcome) --
        # in_flight bookkeeping must not be double-counted by cancel().
        await frontier.mark_written(row.url)
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("done"), 1)
        self.assertNotIn("cancelled", counts)

        await frontier.close()


class TestCancelQuiescence(unittest.IsolatedAsyncioTestCase):
    """The regression class this file exists for: cancelling every
    remaining live row must still let quiescence trigger, with real
    workers polling exactly as they do in production, not just a direct
    counts_by_status() assertion."""

    async def test_cancelling_the_rest_of_the_frontier_lets_quiescence_trigger(self):
        frontier = Frontier(":memory:", follow_gate_exempt_depth=0)
        await frontier.open()
        content_queue: asyncio.Queue = asyncio.Queue(maxsize=4)

        async def crawl_worker():
            while True:
                if frontier.quiescent.is_set():
                    return
                row = await frontier.claim()
                if row is None:
                    if frontier.quiescent.is_set():
                        return
                    await asyncio.sleep(0.005)
                    continue
                await asyncio.sleep(0.02)  # widen the window cancel() needs to land in, deterministically
                await frontier.insert_children(row.url, row.depth, [(c, None) for c in CHILDREN[row.url]])
                await frontier.put_content(content_queue, row.url)

        async def sink_worker():
            while True:
                try:
                    url = await asyncio.wait_for(content_queue.get(), timeout=0.005)
                except asyncio.TimeoutError:
                    if frontier.quiescent.is_set():
                        return
                    continue
                await frontier.score_and_resolve_children(url, score=1.0, promote=True)
                await frontier.mark_extract_outcome(url, "skipped_extract")
                await frontier.content_done(content_queue)

        await frontier.seed([(f"{HOST}/a", None)])
        tasks = [asyncio.create_task(crawl_worker()), asyncio.create_task(sink_worker())]

        # Deterministically land the cancel while b is still live (queued or
        # in_progress, not yet terminal) rather than racing a fixed sleep
        # against however fast the workers happen to run this graph.
        while True:
            cur = await frontier._conn.execute("SELECT status FROM frontier WHERE url = ?", (f"{HOST}/b",))
            row = await cur.fetchone()
            if row is not None and row[0] in ("queued", "in_progress"):
                break
            await asyncio.sleep(0.001)
        await frontier.cancel(url=f"{HOST}/b")

        await asyncio.wait_for(frontier.quiescent.wait(), timeout=10)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

        counts = await frontier.counts_by_status()
        self.assertNotIn("queued", counts)
        self.assertNotIn("pending_score", counts)
        self.assertNotIn("in_progress", counts)

        # The actual invariant, independent of exactly which instant cancel()
        # landed in (b still 'queued', vs. already 'in_progress' with its own
        # children not yet inserted): d and e -- b's subtree -- must never
        # have been processed. Either they never entered the frontier at all
        # (blocked at insert_children -- the in_progress-at-cancel-time
        # case) or they were inserted while still queued and got cancelled
        # directly. What must NOT happen is either one reaching done/
        # skipped_extract, which is what "at least one cancelled row
        # survives" was trying to approximate but is itself timing-dependent
        # (b finishing naturally after being cancelled in_progress
        # overwrites its own row back to skipped_extract -- see
        # Frontier.cancel()'s docstring -- so b itself isn't a reliable
        # witness here, only its blocked descendants are).
        all_urls = await frontier.get_all_urls()
        for descendant in (f"{HOST}/d", f"{HOST}/e"):
            if descendant in all_urls:
                cur = await frontier._conn.execute(
                    "SELECT status FROM frontier WHERE url = ?", (descendant,)
                )
                row = await cur.fetchone()
                self.assertEqual(
                    row[0], "cancelled",
                    f"{descendant} should never be processed once its parent's subtree was cancelled",
                )

        # The untouched branch (c -> f, g) must complete normally --
        # cancelling b's subtree shouldn't collaterally damage it. (Total
        # row count isn't fixed at 7: if cancel() lands before b is ever
        # claimed, or before it inserts children, d/e never enter the
        # frontier at all -- see the descendant loop above.)
        for untouched in (f"{HOST}/c", f"{HOST}/f", f"{HOST}/g"):
            cur = await frontier._conn.execute("SELECT status FROM frontier WHERE url = ?", (untouched,))
            row = await cur.fetchone()
            self.assertEqual(row[0], "skipped_extract", untouched)

        await frontier.close()


class TestRequeueForExtraction(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frontier = Frontier(":memory:", follow_gate_exempt_depth=0)
        await self.frontier.open()
        await self.frontier.seed([(f"{HOST}/a", None)])

    async def asyncTearDown(self):
        await self.frontier.close()

    async def _map_next(self):
        """Claims whichever queued row comes back next, maps it (insert
        children, promote all, mark 'mapped'), returns the row."""
        row = await self.frontier.claim()
        await self.frontier.insert_children(row.url, row.depth, [(c, None) for c in CHILDREN[row.url]])
        await self.frontier.resolve_children(row.url, promote=True)
        await self.frontier.mark_mapped(row.url)
        return row

    async def test_select_by_prefix_requeues_only_mapped_descendants(self):
        await self._map_next()  # a
        await self._map_next()  # one of b/c
        await self._map_next()  # the other of b/c
        # d, e, f, g are 'queued' (never claimed/mapped)

        requeued = await self.frontier.requeue_for_extraction(prefix=f"{HOST}/b")
        self.assertEqual(set(requeued), {f"{HOST}/b"})  # d, e were never 'mapped'

        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("mapped"), 2)  # a, c still mapped
        self.assertGreaterEqual(counts.get("queued", 0), 1)

    async def test_select_does_not_cross_a_cancelled_boundary(self):
        await self._map_next()  # a
        await self._map_next()  # b or c
        await self._map_next()  # the other of b/c -- both expanded so d definitely exists
        await self.frontier.cancel(url=f"{HOST}/d")  # d is 'queued' at this point -> cancelled

        requeued = await self.frontier.requeue_for_extraction(url=f"{HOST}/a")
        self.assertNotIn(f"{HOST}/d", requeued)
        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("cancelled"), 1)

    async def test_requeue_unlatches_quiescence_and_stops_at_cancelled_nodes(self):
        await self._map_next()  # a: b, c auto-queued (depth-0 exempt)
        await self.frontier.cancel(prefix=f"{HOST}/c")  # kill that whole branch
        await self._map_next()  # b: inserts + resolves d, e as queued
        await self.frontier.cancel(url=f"{HOST}/e")  # queued -> cancelled
        await self._map_next()  # d: last live row -> frontier goes quiescent

        self.assertTrue(self.frontier.quiescent.is_set())

        requeued = await self.frontier.requeue_for_extraction(url=f"{HOST}/a")
        self.assertEqual(set(requeued), {f"{HOST}/a", f"{HOST}/b", f"{HOST}/d"})
        self.assertFalse(self.frontier.quiescent.is_set())

        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("cancelled"), 2)  # c, e untouched by the select


if __name__ == "__main__":
    unittest.main()
