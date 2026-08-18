"""Tests for progress_display.py -- read-only, offline. Confirms the
render function doesn't crash on real counts and that the loop actually
terminates on quiescence rather than running forever."""
import asyncio
import unittest

from frontier import Frontier
from progress_display import _render, run_progress_display

SEED = "https://x.test/a"


class TestRender(unittest.TestCase):
    def test_render_with_empty_counts(self):
        table = _render({}, [], started_at=0)
        self.assertIsNotNone(table)

    def test_render_with_in_progress_urls(self):
        table = _render(
            {"queued": 5, "in_progress": 2, "done": 10, "failed": 1},
            ["https://x.test/a", "https://x.test/b"],
            started_at=0,
        )
        self.assertIsNotNone(table)


class TestRunProgressDisplay(unittest.IsolatedAsyncioTestCase):
    async def test_exits_once_frontier_is_quiescent(self):
        frontier = Frontier(":memory:")
        await frontier.open()
        await frontier.seed([(SEED, None)])
        row = await frontier.claim()
        await frontier.mark_written(row.url)  # only page done -> quiescent

        # quiescent is already set by the time we start the display loop
        await asyncio.wait_for(
            run_progress_display(frontier, refresh_interval=0.01), timeout=2
        )
        await frontier.close()


if __name__ == "__main__":
    unittest.main()
