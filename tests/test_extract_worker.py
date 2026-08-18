"""extract_worker failure-mode tests. Offline: stub score_fn/extract_fn,
no LLM calls, no network.

A retried extraction (malformed JSON, rate limit) requeues the row to
'queued' rather than re-pushing content directly -- by design, a retry
re-fetches too, not just re-extracts (see CLAUDE.md / LESSONS_LEARNED.md).
So exercising a real retry cycle needs *something* claiming 'queued' rows
and re-supplying content_queue, same as a real crawl_worker would -- these
tests use a trivial recrawl_stub for that, rather than a real crawl_worker
(which would drag in scope_check/fetch_fn concerns unrelated to what's
being tested here).
"""
import asyncio
import time
import unittest

from frontier import Frontier
from pipeline import extract_worker, RateLimitError, ExtractionResult

SEED = "https://x.test/a"
CONTENT = "markdown content"


async def recrawl_stub(frontier, content_queue, content_by_url, poll_interval=0.01):
    """Simulates an instant, always-successful crawl_worker: claims a
    queued row and immediately supplies its (fixed) content -- just enough
    to let extract_worker's retry-then-refetch cycle actually run."""
    while True:
        row = await frontier.claim()
        if row is None:
            if frontier.quiescent.is_set():
                return
            await asyncio.sleep(poll_interval)
            continue
        await frontier.put_content(content_queue, (row.url, content_by_url[row.url]))


async def run_to_quiescence(tasks, frontier, timeout=5):
    await asyncio.wait_for(frontier.quiescent.wait(), timeout=timeout)
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass


class TestExtractWorkerFailureModes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frontier = Frontier(":memory:", max_retries=3)
        await self.frontier.open()
        await self.frontier.seed([(SEED, None)])
        self.content_queue = asyncio.Queue(maxsize=4)
        self.results_queue = asyncio.Queue(maxsize=4)

    async def asyncTearDown(self):
        await self.frontier.close()

    def _spawn(self, score_fn, extract_fn, extract_threshold=0.5, follow_threshold=0.5,
               chunk_fn=None, extraction_units_fn=None):
        extract_task = asyncio.create_task(
            extract_worker(
                self.frontier, self.content_queue, self.results_queue,
                score_fn, extract_fn, extract_threshold, follow_threshold,
                chunk_fn=chunk_fn, extraction_units_fn=extraction_units_fn, poll_interval=0.01,
            )
        )
        recrawl_task = asyncio.create_task(
            recrawl_stub(self.frontier, self.content_queue, {SEED: CONTENT}, poll_interval=0.01)
        )
        return [extract_task, recrawl_task]

    async def test_malformed_json_prose_wrapped_is_salvaged_not_a_failure(self):
        # Nothing drains results_queue in this test, so quiescence is never
        # reached (results-queue obligation stays outstanding by design) --
        # poll for the result directly instead of waiting for quiescence.
        async def score_fn(url, content):
            return 1.0

        async def extract_fn(content):
            return 'Here is what I found:\n\n[{"instruction": "Q", "response": "A"}]\n\nHope that helps!'

        tasks = self._spawn(score_fn, extract_fn)
        for _ in range(50):
            if self.results_queue.qsize() >= 1:
                break
            await asyncio.sleep(0.02)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        self.assertEqual(self.results_queue.qsize(), 1)
        result: ExtractionResult = self.results_queue.get_nowait()
        self.assertEqual(len(result.qa_pairs), 1)
        pair = result.qa_pairs[0]
        self.assertEqual(pair["question"], "Q")
        self.assertEqual(pair["answer"], "A")
        self.assertEqual(pair["source_chunk"], CONTENT)
        self.assertEqual(pair["source_url"], SEED)
        counts = await self.frontier.counts_by_status()
        self.assertNotIn("failed", counts)

    async def test_malformed_json_unsalvageable_retries_then_fails(self):
        attempts = []

        async def score_fn(url, content):
            return 1.0

        async def extract_fn(content):
            attempts.append(1)
            return "I'm unable to extract structured data from this content."

        tasks = self._spawn(score_fn, extract_fn)
        await run_to_quiescence(tasks, self.frontier)

        self.assertEqual(len(attempts), 3)  # MAX_RETRIES=3
        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("failed"), 1)
        self.assertEqual(self.results_queue.qsize(), 0)

    async def test_rate_limit_sets_not_before_instead_of_sleeping_worker(self):
        """The regression this guards against: a 429 must not block the
        worker slot for the backoff duration. Prove it by asserting the
        worker is still responsive (processes a second, unrelated item)
        well before the backoff would have elapsed if it had slept."""
        calls = {"n": 0}
        BACKOFF = 5.0  # deliberately much larger than the test's own timeout

        async def score_fn(url, content):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimitError(retry_after=BACKOFF)
            return 1.0

        async def extract_fn(content):
            return '[{"instruction": "Q", "response": "A"}]'

        tasks = self._spawn(score_fn, extract_fn)

        start = time.monotonic()
        # Row gets rate-limited, released with not_before far in the
        # future -- claim() must find nothing else to do and the worker
        # must NOT be blocked processing it. Confirm the row is back in
        # 'queued' (not in_progress, not stuck mid-worker) well before
        # BACKOFF would have elapsed.
        for _ in range(20):
            counts = await self.frontier.counts_by_status()
            if counts.get("queued") == 1 and "in_progress" not in counts:
                break
            await asyncio.sleep(0.02)
        elapsed = time.monotonic() - start

        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        self.assertLess(
            elapsed, BACKOFF,
            "row wasn't released back to queued promptly -- worker may be "
            "blocking on the backoff instead of releasing via not_before",
        )
        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 1)
        self.assertNotIn("in_progress", counts)

    async def test_score_below_extract_threshold_is_skipped_not_failed(self):
        async def score_fn(url, content):
            return 0.1

        async def extract_fn(content):
            raise AssertionError("extract_fn must not be called below extract_threshold")

        tasks = self._spawn(score_fn, extract_fn)
        await run_to_quiescence(tasks, self.frontier)

        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("skipped_extract"), 1)
        self.assertNotIn("failed", counts)

    async def test_chunk_fn_result_attached_to_extraction_result_when_provided(self):
        async def score_fn(url, content):
            return 1.0

        async def extract_fn(content):
            return '[{"instruction": "Q", "response": "A"}]'

        def chunk_fn(content):
            return [{"text": content, "parent_text": content}]

        tasks = self._spawn(score_fn, extract_fn, chunk_fn=chunk_fn)
        for _ in range(50):
            if self.results_queue.qsize() >= 1:
                break
            await asyncio.sleep(0.02)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        result: ExtractionResult = self.results_queue.get_nowait()
        self.assertEqual(result.chunks, [{"text": CONTENT, "parent_text": CONTENT}])

    async def test_per_chunk_extraction_calls_extract_fn_once_per_unit_and_tags_source_chunk(self):
        async def score_fn(url, content):
            return 1.0

        calls = []

        async def extract_fn(unit):
            calls.append(unit)
            return f'[{{"instruction": "Q about {unit}", "response": "A"}}]'

        async def extraction_units_fn(content):
            return ["chunk-1", "chunk-2", "chunk-3"]

        tasks = self._spawn(score_fn, extract_fn, extraction_units_fn=extraction_units_fn)
        for _ in range(50):
            if self.results_queue.qsize() >= 1:
                break
            await asyncio.sleep(0.02)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        self.assertEqual(calls, ["chunk-1", "chunk-2", "chunk-3"])
        result: ExtractionResult = self.results_queue.get_nowait()
        self.assertEqual(len(result.qa_pairs), 3)
        self.assertEqual([p["source_chunk"] for p in result.qa_pairs], ["chunk-1", "chunk-2", "chunk-3"])

    async def test_one_malformed_unit_among_several_does_not_fail_the_page(self):
        async def score_fn(url, content):
            return 1.0

        async def extract_fn(unit):
            if unit == "bad-chunk":
                return "I can't extract anything from this."
            return '[{"instruction": "Q", "response": "A"}]'

        async def extraction_units_fn(content):
            return ["good-chunk", "bad-chunk"]

        tasks = self._spawn(score_fn, extract_fn, extraction_units_fn=extraction_units_fn)
        for _ in range(50):
            if self.results_queue.qsize() >= 1:
                break
            await asyncio.sleep(0.02)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        result: ExtractionResult = self.results_queue.get_nowait()
        self.assertEqual(len(result.qa_pairs), 1)  # only the good chunk's pair
        counts = await self.frontier.counts_by_status()
        self.assertNotIn("failed", counts)

    async def test_all_units_malformed_fails_the_page(self):
        attempts = []

        async def score_fn(url, content):
            return 1.0

        async def extract_fn(unit):
            attempts.append(unit)
            return "I can't extract anything from this."

        async def extraction_units_fn(content):
            return ["chunk-1", "chunk-2"]

        tasks = self._spawn(score_fn, extract_fn, extraction_units_fn=extraction_units_fn)
        await run_to_quiescence(tasks, self.frontier)

        self.assertEqual(len(attempts), 6)  # MAX_RETRIES=3 x 2 units/attempt
        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("failed"), 1)
        self.assertEqual(self.results_queue.qsize(), 0)

    async def test_no_chunk_fn_leaves_chunks_none(self):
        async def score_fn(url, content):
            return 1.0

        async def extract_fn(content):
            return '[{"instruction": "Q", "response": "A"}]'

        tasks = self._spawn(score_fn, extract_fn)  # chunk_fn not provided
        for _ in range(50):
            if self.results_queue.qsize() >= 1:
                break
            await asyncio.sleep(0.02)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        result: ExtractionResult = self.results_queue.get_nowait()
        self.assertIsNone(result.chunks)


if __name__ == "__main__":
    unittest.main()
