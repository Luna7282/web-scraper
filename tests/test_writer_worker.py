"""writer_worker loop tests -- frontier integration (mark_written /
mark_write_failed), not Writer's own internals (see tests/test_writer.py
for those). Offline: temp JSONL file, no network."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from frontier import Frontier
from pipeline import writer_worker, ExtractionResult
from writer import Writer

SEED = "https://x.test/a"


class TestWriterWorker(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.jsonl_path = str(Path(self._tmpdir.name) / "unified.jsonl")
        self.frontier = Frontier(":memory:")
        await self.frontier.open()
        await self.frontier.seed([(SEED, None)])
        await self.frontier.claim()  # as if crawled + extracted already
        self.results_queue = asyncio.Queue(maxsize=4)

    async def asyncTearDown(self):
        await self.frontier.close()
        self._tmpdir.cleanup()

    async def test_successful_write_marks_done(self):
        writer = Writer(self.jsonl_path)
        await self.frontier.put_results(
            self.results_queue,
            ExtractionResult(url=SEED, qa_pairs=[{"instruction": "Q", "response": "A"}]),
        )
        task = asyncio.create_task(
            writer_worker(self.frontier, self.results_queue, writer, poll_interval=0.01)
        )
        await asyncio.wait_for(self.frontier.quiescent.wait(), timeout=5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("done"), 1)
        self.assertIn("Q", Path(self.jsonl_path).read_text(encoding="utf-8"))

    async def test_write_failure_with_retries_exhausted_marks_failed(self):
        # max_retries=1 here: a write failure requeues to 'queued' rather
        # than failing outright (see pipeline.py), and nothing re-supplies
        # results_queue without a full crawl+extract cycle -- out of scope
        # for a writer-focused test. A single-retry budget lets one
        # failure reach the cap directly, still exercising the real
        # retry-or-fail path without needing that machinery.
        await self.frontier.close()
        self.frontier = Frontier(":memory:", max_retries=1)
        await self.frontier.open()
        await self.frontier.seed([(SEED, None)])
        await self.frontier.claim()

        class AlwaysFailsWriter:
            async def write(self, url, qa_pairs, chunks=None):
                raise IOError("disk full")

        await self.frontier.put_results(
            self.results_queue,
            ExtractionResult(url=SEED, qa_pairs=[{"instruction": "Q", "response": "A"}]),
        )
        task = asyncio.create_task(
            writer_worker(self.frontier, self.results_queue, AlwaysFailsWriter(), poll_interval=0.01)
        )
        await asyncio.wait_for(self.frontier.quiescent.wait(), timeout=5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        counts = await self.frontier.counts_by_status()
        self.assertEqual(counts.get("failed"), 1)
        self.assertNotIn("done", counts)
        self.assertFalse(Path(self.jsonl_path).exists())


if __name__ == "__main__":
    unittest.main()
