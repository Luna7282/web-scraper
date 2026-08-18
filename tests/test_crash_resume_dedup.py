"""Verifies the exact scenario step 8's kill test will exercise for real:
a writer crash and resume must produce neither duplicate JSONL rows nor
duplicate Chroma vectors, even though the two dedup mechanisms are
different (JSONL: file-layer source_url marker, step 5; Chroma:
content-hash id upsert, this step) and live in different storage systems
with no shared transaction between them.

Offline: temp JSONL file + chromadb.EphemeralClient(), stub embed_fn.
"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path

import chromadb

from chunk_store import ChunkStore, get_or_create_collection, chunk_id
from writer import Writer

URL = "https://x.com/install/linux"
QA_PAIRS = [{"instruction": "How do I install X?", "response": "Run these steps."}]
CHUNKS = [
    {"text": "shared install instructions repeated on every OS page", "parent_text": "parent block A"},
    {"text": "linux-specific step", "parent_text": "parent block A"},
]


def make_stub_embed_fn():
    calls = []
    async def embed_fn(text: str) -> list[float]:
        calls.append(text)
        return [float(len(text)), 0.0]
    embed_fn.calls = calls
    return embed_fn


class TestCrashResumeDedup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.jsonl_path = str(Path(self._tmpdir.name) / "unified.jsonl")
        self.client = chromadb.EphemeralClient()
        self.collection = get_or_create_collection(self.client, f"test_{uuid.uuid4().hex}", "fake-model", 2)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_writer(self, embed_fn):
        store = ChunkStore(self.collection, embed_fn)

        async def chroma_upsert_fn(url: str, chunks: list[dict]) -> None:
            for c in chunks:
                await store.add_or_merge_chunk(c["text"], url, c["parent_text"])

        return Writer(self.jsonl_path, chroma_upsert_fn=chroma_upsert_fn)

    async def test_full_success_then_retry_produces_no_duplicates_anywhere(self):
        """The common case: write() succeeds completely once, then gets
        called again anyway (e.g. a crash-recovery reset that turned out
        to be unnecessary, or any other reason the pipeline retries a
        page whose write already fully succeeded)."""
        embed_fn = make_stub_embed_fn()

        # First attempt: full success.
        writer1 = self._make_writer(embed_fn)
        await writer1.write(URL, QA_PAIRS, CHUNKS)

        # Simulate resume: fresh Writer instance (as a real crash-restart
        # would construct), same jsonl_path (preload picks up prior state)
        # and same Chroma collection (persisted, unlike the in-memory
        # written_urls set).
        writer2 = self._make_writer(embed_fn)
        await writer2.write(URL, QA_PAIRS, CHUNKS)

        # JSONL: exactly one row, not two.
        lines = Path(self.jsonl_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)

        # Chroma: exactly 2 vectors (one per chunk), not 4.
        self.assertEqual(self.collection.count(), 2)

        # No re-embedding on the second attempt -- both chunks already
        # existed with this exact source, so the retry should be a pure
        # metadata-read with no new embed calls.
        self.assertEqual(len(embed_fn.calls), 2)  # only the first attempt's 2 chunks

        # Sources list has each url exactly once, not duplicated.
        for chunk in CHUNKS:
            cid = chunk_id(chunk["text"])
            got = self.collection.get(ids=[cid])
            self.assertEqual(got["metadatas"][0]["sources"], [URL])

    async def test_write_crash_between_jsonl_and_chroma_then_retry_completes_safely(self):
        """The actual crash window LESSONS_LEARNED #13 is about: JSONL
        append succeeds, then a crash happens before Chroma upsert runs.
        On retry, JSONL must skip (already written) but Chroma must still
        receive the chunks -- Writer.write() already does this (chroma
        upsert isn't gated on the JSONL already_written check), confirmed
        here rather than assumed."""
        embed_fn = make_stub_embed_fn()

        # Simulate the crash: manually pre-seed the JSONL file (as if the
        # append succeeded) without ever touching Chroma.
        Path(self.jsonl_path).write_text(
            json.dumps({**QA_PAIRS[0], "source_url": URL}) + "\n", encoding="utf-8"
        )
        self.assertEqual(self.collection.count(), 0)  # confirm Chroma really is empty

        # Resume: retry the full write.
        writer = self._make_writer(embed_fn)
        await writer.write(URL, QA_PAIRS, CHUNKS)

        lines = Path(self.jsonl_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)  # still just the one row from "before the crash"
        self.assertEqual(self.collection.count(), 2)  # but Chroma now has both chunks

    async def test_cross_page_duplicate_chunk_survives_a_retry_on_either_page(self):
        """Combines both dedup mechanisms with a real cross-page-duplicate
        chunk (the install-instructions case) -- write page A, retry page
        A, then write page B sharing one chunk with A. Exactly one vector
        for the shared chunk, with both source URLs recorded, and no
        duplicate JSONL rows for either page."""
        embed_fn = make_stub_embed_fn()
        writer = self._make_writer(embed_fn)

        url_a, url_b = "https://x.com/linux", "https://x.com/macos"
        shared_chunk = {"text": "identical install text", "parent_text": "p"}
        qa_a = [{"instruction": "Q-linux", "response": "A-linux"}]
        qa_b = [{"instruction": "Q-macos", "response": "A-macos"}]

        await writer.write(url_a, qa_a, [shared_chunk])
        await writer.write(url_a, qa_a, [shared_chunk])  # retry of A
        await writer.write(url_b, qa_b, [shared_chunk])  # different page, same chunk

        lines = Path(self.jsonl_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)  # one row per page, not per write() call

        self.assertEqual(self.collection.count(), 1)  # one vector for the shared chunk
        cid = chunk_id(shared_chunk["text"])
        sources = self.collection.get(ids=[cid])["metadatas"][0]["sources"]
        self.assertEqual(set(sources), {url_a, url_b})
        self.assertEqual(len(embed_fn.calls), 1)  # embedded once, ever


if __name__ == "__main__":
    unittest.main()
