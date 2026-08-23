"""crawl/pipeline.py::map_worker + the full map-then-select-then-extract
workflow (CLAUDE.md item 1's "that's the whole workflow"). Offline,
synthetic graph, real crawl_worker + real map_worker + real Frontier --
same StubFetcher pattern as test_crawl_worker.py, no crawl4ai, no network,
no LLM calls (map mode takes none by construction: map_worker's signature
has no score_fn/extract_fn parameter at all).

Graph (~12 nodes, deep enough to prove max_pages/max_depth caps and give
a real pages/min number, not a toy 3-node one):

    root
    +-- s1 --+-- s1/1
    |        +-- s1/2 --+-- s1/2/1
    |                   +-- s1/2/2
    +-- s2 --+-- s2/1
    |        +-- s2/2
    +-- s3 --+-- s3/1
             +-- s3/2
"""
import asyncio
import time
import unittest

from crawl.frontier import Frontier
from crawl.pipeline import crawl_worker, map_worker, extract_worker, FetchTimeout
from crawl.scope import normalize_url, is_in_scope

HOST = "https://site.test"
ROOT = f"{HOST}/"
LINKS = {
    ROOT: [f"{HOST}/s1", f"{HOST}/s2", f"{HOST}/s3"],
    f"{HOST}/s1": [f"{HOST}/s1/1", f"{HOST}/s1/2"],
    f"{HOST}/s1/2": [f"{HOST}/s1/2/1", f"{HOST}/s1/2/2"],
    f"{HOST}/s2": [f"{HOST}/s2/1", f"{HOST}/s2/2"],
    f"{HOST}/s3": [f"{HOST}/s3/1", f"{HOST}/s3/2"],
}
ALL_URLS = {ROOT, *LINKS[ROOT]} | {u for k in LINKS for u in LINKS[k]}


def _make_fetch_fn():
    async def fetch_fn(url: str):
        return f"<html>{url}</html>", LINKS.get(url, [])
    return fetch_fn


def _scope_check(url: str) -> bool:
    return is_in_scope(url, host=HOST.split("//")[1], prefix=None).allowed


class TestMapWorkerOfflineCrawl(unittest.IsolatedAsyncioTestCase):
    async def test_full_map_reaches_every_reachable_url_as_mapped_no_score_no_extract(self):
        frontier = Frontier(":memory:", follow_gate_exempt_depth=0)
        await frontier.open()
        await frontier.seed([(ROOT, None)])

        content_queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        fetch_fn = _make_fetch_fn()

        started = time.monotonic()
        tasks = [
            asyncio.create_task(crawl_worker(frontier, fetch_fn, content_queue, _scope_check))
            for _ in range(3)
        ] + [asyncio.create_task(map_worker(frontier, content_queue)) for _ in range(2)]

        await asyncio.wait_for(frontier.quiescent.wait(), timeout=10)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        elapsed = time.monotonic() - started

        counts = await frontier.counts_by_status()
        self.assertEqual(counts, {"mapped": len(ALL_URLS)})
        # No relevance score was ever computed -- map mode has no score_fn.
        scores = await frontier.all_scores()
        self.assertEqual(scores, [])

        pages_per_min = len(ALL_URLS) / max(elapsed / 60, 1e-9)
        print(f"\n[map demo] {len(ALL_URLS)} pages mapped in {elapsed:.3f}s ({pages_per_min:.0f} pages/min)")

        await frontier.close()

    async def test_max_pages_caps_the_map_pass_same_as_a_full_crawl(self):
        frontier = Frontier(":memory:", max_pages=4, follow_gate_exempt_depth=0)
        await frontier.open()
        await frontier.seed([(ROOT, None)])
        content_queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        fetch_fn = _make_fetch_fn()

        tasks = [
            asyncio.create_task(crawl_worker(frontier, fetch_fn, content_queue, _scope_check))
            for _ in range(3)
        ] + [asyncio.create_task(map_worker(frontier, content_queue))]

        await asyncio.wait_for(frontier.quiescent.wait(), timeout=10)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

        counts = await frontier.counts_by_status()
        # Soft cap under concurrency, same as done/skipped_extract already
        # were before 'mapped' existed: claim()'s budget check counts only
        # already-terminal rows, so a few workers can claim past the cap
        # before the mapped-count catches up. The invariant that matters is
        # "stopped meaningfully short of the full graph", not an exact 4 --
        # proving 'mapped' is actually wired into the same cap-counting
        # path 'done'/'skipped_extract' use, not just claim()-reachable.
        self.assertGreaterEqual(counts.get("mapped", 0), 4)
        self.assertLess(counts.get("mapped", 0), len(ALL_URLS))
        # max_pages leaves queued rows live by design (CLAUDE.md) -- proves
        # 'mapped' was actually wired into the same cap-counting path 'done'/
        # 'skipped_extract' already use, not just claim()-reachable by luck.
        self.assertGreater(counts.get("queued", 0) + counts.get("pending_score", 0), 0)

        await frontier.close()


class TestMapThenSelectThenExtractWorkflow(unittest.IsolatedAsyncioTestCase):
    """The whole point of map mode per CLAUDE.md: map first, select a
    subtree, then extract only that subtree on a resumed run -- proven
    end to end against the same frontier.db-equivalent (here, the same
    Frontier instance, since content is never persisted either way)."""

    async def test_select_prefix_then_real_extraction_only_touches_selected_subtree(self):
        frontier = Frontier(":memory:", follow_gate_exempt_depth=0)
        await frontier.open()
        await frontier.seed([(ROOT, None)])
        content_queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        fetch_fn = _make_fetch_fn()

        # Phase 1: map everything.
        tasks = [
            asyncio.create_task(crawl_worker(frontier, fetch_fn, content_queue, _scope_check))
            for _ in range(3)
        ] + [asyncio.create_task(map_worker(frontier, content_queue))]
        await asyncio.wait_for(frontier.quiescent.wait(), timeout=10)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        self.assertEqual((await frontier.counts_by_status()).get("mapped"), len(ALL_URLS))

        # Phase 2: select just the s2 subtree for real extraction.
        requeued = await frontier.requeue_for_extraction(prefix=f"{HOST}/s2")
        self.assertEqual(set(requeued), {f"{HOST}/s2", f"{HOST}/s2/1", f"{HOST}/s2/2"})
        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("queued"), 3)
        self.assertEqual(counts.get("mapped"), len(ALL_URLS) - 3)

        # Phase 3: a real (stubbed) extraction pass over the same frontier
        # -- selected pages re-fetch (content wasn't persisted from the
        # map pass) and this time go through score_fn/extract_fn for real.
        extracted_urls: list[str] = []

        async def score_fn(url, content):
            return 1.0

        async def extract_fn(content):
            extracted_urls.append(content)
            return '[{"instruction": "q", "response": "a"}]'

        content_queue2: asyncio.Queue = asyncio.Queue(maxsize=8)
        results_queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        tasks = [
            asyncio.create_task(crawl_worker(frontier, fetch_fn, content_queue2, _scope_check))
            for _ in range(2)
        ] + [
            asyncio.create_task(
                extract_worker(frontier, content_queue2, results_queue, score_fn, extract_fn,
                                extract_threshold=0.0, follow_threshold=0.0)
            )
        ]
        # No writer_worker: drain results_queue directly, mirroring
        # writer_worker's own frontier calls (mark_written/results_done) --
        # test_writer_ownership.py's structural check means this test
        # cannot construct a real Writer without also using writer_worker,
        # and a stub is unnecessary here since only frontier state matters.
        async def drain_results():
            while True:
                try:
                    result = await asyncio.wait_for(results_queue.get(), timeout=0.02)
                except asyncio.TimeoutError:
                    if frontier.quiescent.is_set():
                        return
                    continue
                await frontier.mark_written(result.url)
                await frontier.results_done(results_queue)

        tasks.append(asyncio.create_task(drain_results()))
        await asyncio.wait_for(frontier.quiescent.wait(), timeout=10)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

        counts = await frontier.counts_by_status()
        self.assertEqual(counts.get("done"), 3)  # exactly the selected subtree
        self.assertEqual(counts.get("mapped"), len(ALL_URLS) - 3)  # everything else untouched
        self.assertEqual(len(extracted_urls), 3)

        await frontier.close()


if __name__ == "__main__":
    unittest.main()
