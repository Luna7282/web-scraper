"""Direct unit tests of Frontier's mechanisms, isolated from any worker
loop -- orphan resolution, crash recovery, not_before/backoff, max_pages/
max_depth enforcement, seed exemption. Offline, in-memory SQLite."""
import asyncio
import unittest

from frontier import Frontier

SEED = "https://x.test/a"


class TestOrphanResolution(unittest.IsolatedAsyncioTestCase):
    async def test_permanently_failed_parent_orphans_pending_children(self):
        # follow_gate_exempt_depth=-1: nothing is exempt, so children of
        # the seed (depth 0) go to pending_score, not auto-queued -- needed
        # to actually exercise the orphan path this test targets.
        frontier = Frontier(":memory:", follow_gate_exempt_depth=-1, max_retries=1)
        await frontier.open()
        await frontier.seed([(SEED, None)])
        row = await frontier.claim()
        await frontier.insert_children(row.url, row.depth, [("https://x.test/child", None)])

        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("pending_score"), 1)

        await frontier.mark_fetch_failed(row.url, "simulated permanent failure")

        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("failed"), 1)
        self.assertEqual(counts.get("skipped_follow"), 1)
        self.assertNotIn("pending_score", counts)
        await frontier.close()

    async def test_extraction_failure_also_orphans_children(self):
        # Same rule, the other terminal-failure path (extraction rather
        # than fetch) -- both go through the same _locked_retry_or_fail ->
        # _locked_mark_terminal('failed') route, so one test per entry
        # point is enough to confirm both wire into the same orphan logic.
        frontier = Frontier(":memory:", follow_gate_exempt_depth=-1, max_retries=1)
        await frontier.open()
        await frontier.seed([(SEED, None)])
        row = await frontier.claim()
        await frontier.insert_children(row.url, row.depth, [("https://x.test/child", None)])

        await frontier.mark_extract_outcome(row.url, "failed", "simulated extraction failure")

        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("failed"), 1)
        self.assertEqual(counts.get("skipped_follow"), 1)
        await frontier.close()

    async def test_successful_terminal_states_do_not_orphan(self):
        frontier = Frontier(":memory:", follow_gate_exempt_depth=-1)
        await frontier.open()
        await frontier.seed([(SEED, None)])
        row = await frontier.claim()
        await frontier.insert_children(row.url, row.depth, [("https://x.test/child", None)])

        await frontier.score_and_resolve_children(row.url, score=0.9, promote=True)
        await frontier.mark_written(row.url)

        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("done"), 1)
        self.assertEqual(counts.get("queued"), 1)  # promoted, not orphaned
        await frontier.close()


class TestCrashRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_in_progress_rows_requeued_under_retry_cap(self):
        frontier = Frontier(":memory:", max_retries=3)
        await frontier.open()
        await frontier.seed([(SEED, None)])
        await frontier.claim()  # simulate a crash: leaves this row 'in_progress'

        result = await frontier.recover_crashed()
        self.assertEqual(result, {"requeued": 1, "failed": 0})
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 1)
        await frontier.close()

    async def test_in_progress_rows_at_retry_cap_fail_with_orphan_resolution(self):
        frontier = Frontier(":memory:", follow_gate_exempt_depth=-1, max_retries=1)
        await frontier.open()
        await frontier.seed([(SEED, None)])
        row = await frontier.claim()
        await frontier.insert_children(row.url, row.depth, [("https://x.test/child", None)])
        # row is 'in_progress' with retry_count=0; recover_crashed bumps it
        # to 1, which is >= max_retries=1 -> terminally failed this time.

        result = await frontier.recover_crashed()
        self.assertEqual(result, {"requeued": 0, "failed": 1})
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("failed"), 1)
        self.assertEqual(counts.get("skipped_follow"), 1)
        await frontier.close()

    async def test_recovery_on_clean_frontier_is_a_no_op(self):
        frontier = Frontier(":memory:")
        await frontier.open()
        await frontier.seed([(SEED, None)])  # nothing in_progress
        result = await frontier.recover_crashed()
        self.assertEqual(result, {"requeued": 0, "failed": 0})
        await frontier.close()


class TestBackoff(unittest.IsolatedAsyncioTestCase):
    async def test_not_before_blocks_claim_until_it_passes(self):
        frontier = Frontier(":memory:", max_retries=5)
        await frontier.open()
        await frontier.seed([(SEED, None)])
        row = await frontier.claim()
        await frontier.mark_fetch_failed(row.url, "rate limited", backoff_seconds=10)

        # not_before is 10s out -- must not be claimable right now.
        self.assertIsNone(await frontier.claim())
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 1)  # present, just not claimable yet
        await frontier.close()

    async def test_zero_backoff_claimable_immediately(self):
        frontier = Frontier(":memory:", max_retries=5)
        await frontier.open()
        await frontier.seed([(SEED, None)])
        row = await frontier.claim()
        await frontier.mark_fetch_failed(row.url, "transient", backoff_seconds=0)

        reclaimed = await frontier.claim()
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.url, SEED)
        await frontier.close()


class TestCapsAndExemption(unittest.IsolatedAsyncioTestCase):
    async def test_max_depth_prevents_insertion_beyond_cap(self):
        frontier = Frontier(":memory:", max_depth=1, follow_gate_exempt_depth=-1)
        await frontier.open()
        await frontier.seed([(SEED, None)])  # depth 0
        row = await frontier.claim()
        await frontier.insert_children(row.url, row.depth, [("https://x.test/d1", None)])  # depth 1: OK
        counts = await frontier.counts_by_status()
        self.assertEqual(sum(counts.values()), 2)  # seed + d1 child

        child_row_url = "https://x.test/d1"
        await frontier.score_and_resolve_children(row.url, 0.9, promote=True)
        d1_row = await frontier.claim()
        self.assertEqual(d1_row.url, child_row_url)
        await frontier.insert_children(d1_row.url, d1_row.depth, [("https://x.test/d2", None)])  # depth 2: over cap
        counts = await frontier.counts_by_status()
        self.assertEqual(sum(counts.values()), 2)  # d2 never inserted
        await frontier.close()

    async def test_max_pages_stops_claims_but_leaves_queued_rows_live(self):
        frontier = Frontier(":memory:", max_pages=1)
        await frontier.open()
        await frontier.seed([(SEED, None), ("https://x.test/b", None)])

        first = await frontier.claim()
        self.assertIsNotNone(first)
        await frontier.mark_written(first.url)  # 1 page now done

        second = await frontier.claim()  # cap reached -- must refuse
        self.assertIsNone(second)
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 1)  # left live, not touched
        await frontier.close()

    async def test_seed_children_exempt_by_default_auto_queued(self):
        frontier = Frontier(":memory:")  # default follow_gate_exempt_depth=0
        await frontier.open()
        await frontier.seed([(SEED, None)])
        row = await frontier.claim()
        await frontier.insert_children(row.url, row.depth, [("https://x.test/child", None)])
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 1)
        self.assertNotIn("pending_score", counts)
        await frontier.close()


if __name__ == "__main__":
    unittest.main()
