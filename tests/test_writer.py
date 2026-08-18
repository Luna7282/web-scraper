"""Tests for writer.py -- sole ownership guard, JSONL dedup-by-source_url,
Chroma upsert wiring. Offline: temp files only, no network."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from writer import Writer


class TestWriter(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.jsonl_path = str(Path(self._tmpdir.name) / "unified.jsonl")

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_write_appends_jsonl_with_source_url(self):
        w = Writer(self.jsonl_path)
        await w.write("https://x.com/a", [{"instruction": "Q1", "response": "A1"}])
        lines = Path(self.jsonl_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["source_url"], "https://x.com/a")
        self.assertEqual(row["instruction"], "Q1")

    async def test_preload_marks_existing_source_urls_written(self):
        Path(self.jsonl_path).write_text(
            json.dumps({"instruction": "Q", "response": "A", "source_url": "https://x.com/a"}) + "\n",
            encoding="utf-8",
        )
        w = Writer(self.jsonl_path)
        self.assertTrue(w.already_written("https://x.com/a"))
        self.assertFalse(w.already_written("https://x.com/b"))

    async def test_already_written_url_skips_jsonl_append(self):
        Path(self.jsonl_path).write_text(
            json.dumps({"instruction": "Q", "response": "A", "source_url": "https://x.com/a"}) + "\n",
            encoding="utf-8",
        )
        w = Writer(self.jsonl_path)
        await w.write("https://x.com/a", [{"instruction": "Q2", "response": "A2"}])
        lines = Path(self.jsonl_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)  # not 2 -- the re-attempt was skipped

    async def test_already_written_url_still_upserts_chroma(self):
        upserted = []

        async def chroma_upsert_fn(chunks):
            upserted.append(chunks)

        Path(self.jsonl_path).write_text(
            json.dumps({"instruction": "Q", "response": "A", "source_url": "https://x.com/a"}) + "\n",
            encoding="utf-8",
        )
        w = Writer(self.jsonl_path, chroma_upsert_fn=chroma_upsert_fn)
        await w.write("https://x.com/a", [{"instruction": "Q2", "response": "A2"}], chunks=[{"id": "c1"}])
        self.assertEqual(len(upserted), 1)  # chroma still runs even though JSONL was skipped

    async def test_concurrent_calls_raise_instead_of_silently_serializing(self):
        async def slow_chroma_upsert(chunks):
            await asyncio.sleep(0.05)

        w = Writer(self.jsonl_path, chroma_upsert_fn=slow_chroma_upsert)

        async def call(url):
            await w.write(url, [{"instruction": "Q", "response": "A"}], chunks=[{"id": "c"}])

        with self.assertRaises(RuntimeError):
            await asyncio.gather(call("https://x.com/a"), call("https://x.com/b"))

    async def test_empty_qa_pairs_writes_nothing_but_does_not_error(self):
        w = Writer(self.jsonl_path)
        await w.write("https://x.com/a", [])
        self.assertFalse(Path(self.jsonl_path).exists())


if __name__ == "__main__":
    unittest.main()
