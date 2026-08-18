"""Offline tests for extraction_units.py -- pure chunk_text under the hood
plus a stub embed_fn for the top_k strategy, no network."""
import unittest

from config import ExtractionStrategy
from extraction_units import select_extraction_units


class TestSelectExtractionUnits(unittest.IsolatedAsyncioTestCase):
    async def test_first_n_chars_returns_single_truncated_unit(self):
        content = "x" * 10000
        units = await select_extraction_units(content, ExtractionStrategy.FIRST_N_CHARS, max_chars=100)
        self.assertEqual(units, [content[:100]])

    async def test_per_chunk_covers_content_past_the_old_truncation_window(self):
        content = "A" * 3000 + "B" * 3000  # 6000 chars, well past a 4000-char single window
        units = await select_extraction_units(
            content, ExtractionStrategy.PER_CHUNK, chunk_size=2000, chunk_overlap=0,
        )
        self.assertGreater(len(units), 1)
        self.assertTrue(any("B" in u for u in units))  # content past char 4000 is reachable

    async def test_top_k_without_intent_falls_back_to_per_chunk(self):
        content = "A" * 2000 + "B" * 2000 + "C" * 2000
        units = await select_extraction_units(
            content, ExtractionStrategy.TOP_K_CHUNKS_BY_RELEVANCE,
            chunk_size=2000, chunk_overlap=0, embed_fn=None, intent_embedding=None,
        )
        self.assertEqual(len(units), 3)  # no ranking signal -- complete coverage, not an arbitrary subset

    async def test_top_k_with_intent_keeps_only_the_k_most_similar_chunks(self):
        chunks_by_text = {"A" * 2000: [1.0, 0.0], "B" * 2000: [0.0, 1.0], "C" * 2000: [0.5, 0.5]}
        content = "".join(chunks_by_text.keys())

        async def embed_fn(text):
            return chunks_by_text[text]

        units = await select_extraction_units(
            content, ExtractionStrategy.TOP_K_CHUNKS_BY_RELEVANCE,
            chunk_size=2000, chunk_overlap=0, embed_fn=embed_fn,
            intent_embedding=[1.0, 0.0], top_k=1,
        )
        self.assertEqual(units, ["A" * 2000])  # exact match to the intent embedding wins

    async def test_empty_content_returns_no_units_for_chunked_strategies(self):
        units = await select_extraction_units("", ExtractionStrategy.PER_CHUNK)
        self.assertEqual(units, [])

    async def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            await select_extraction_units("text", "not_a_real_strategy")


if __name__ == "__main__":
    unittest.main()
