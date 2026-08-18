"""Structural verification that Writer has exactly one caller.

This is the actual reason the old asyncio.Lock-per-write pattern from
output_manager.py goes away -- worth an explicit, permanent check rather
than a one-time assumption that could silently regress later if someone
adds a new parameter to crawl_worker or extract_worker without noticing
it's a Writer/Chroma reference.
"""
import inspect
import unittest

from crawl import pipeline


class TestWriterSoleOwnership(unittest.TestCase):
    def test_crawl_worker_signature_cannot_receive_writer_or_chroma(self):
        params = [p.lower() for p in inspect.signature(pipeline.crawl_worker).parameters]
        self.assertNotIn("writer", params)
        self.assertFalse(any("chroma" in p for p in params))

    def test_extract_worker_signature_cannot_receive_writer_or_chroma(self):
        params = [p.lower() for p in inspect.signature(pipeline.extract_worker).parameters]
        self.assertNotIn("writer", params)
        self.assertFalse(any("chroma" in p for p in params))

    def test_writer_worker_is_the_only_function_taking_a_writer_param(self):
        for name in ("crawl_worker", "extract_worker", "writer_worker"):
            params = [p.lower() for p in inspect.signature(getattr(pipeline, name)).parameters]
            has_writer = "writer" in params
            self.assertEqual(
                has_writer, name == "writer_worker",
                f"{name} {'unexpectedly has' if has_writer else 'is missing'} a writer parameter",
            )


if __name__ == "__main__":
    unittest.main()
