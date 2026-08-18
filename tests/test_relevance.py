"""Offline tests for relevance.py's pure logic and scoring mechanics --
stub embed_fn with deterministic fake vectors. Real semantic behavior
(does this actually discriminate relevant from irrelevant pages) is
measured separately against real embeddings, not asserted here -- these
tests only prove the math and the strategies wire together correctly."""
import unittest

from relevance import (
    cosine_similarity, extract_headings, chunk_text,
    score_whole_page, score_headings, score_max_chunk, make_score_fn,
)


class TestCosineSimilarity(unittest.TestCase):
    def test_identical_vectors_score_one(self):
        self.assertAlmostEqual(cosine_similarity([1, 0, 0], [1, 0, 0]), 1.0)

    def test_orthogonal_vectors_score_zero(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_opposite_vectors_score_negative_one(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [-1, 0]), -1.0)

    def test_zero_vector_scores_zero_not_nan(self):
        self.assertEqual(cosine_similarity([0, 0], [1, 1]), 0.0)

    def test_scale_invariant(self):
        a = cosine_similarity([1, 2, 3], [4, 5, 6])
        b = cosine_similarity([10, 20, 30], [4, 5, 6])
        self.assertAlmostEqual(a, b)


class TestExtractHeadings(unittest.TestCase):
    def test_extracts_atx_headings_only(self):
        md = "# Title\n\nSome prose.\n\n## Subheading\n\nMore prose.\n"
        self.assertEqual(extract_headings(md), "# Title\n## Subheading")

    def test_no_headings_returns_empty_string(self):
        self.assertEqual(extract_headings("just prose, no headings at all"), "")

    def test_indented_headings_still_captured(self):
        md = "  # Indented title\ntext"
        self.assertEqual(extract_headings(md), "# Indented title")


class TestChunkText(unittest.TestCase):
    def test_short_text_single_chunk(self):
        chunks = chunk_text("short text", chunk_size=2000)
        self.assertEqual(chunks, ["short text"])

    def test_long_text_multiple_chunks(self):
        text = "word " * 1000  # ~5000 chars
        chunks = chunk_text(text, chunk_size=500, chunk_overlap=0)
        self.assertGreater(len(chunks), 1)

    def test_empty_text_no_chunks(self):
        self.assertEqual(chunk_text(""), [])


def make_stub_embed_fn(vectors: dict[str, list[float]], default=None):
    async def embed_fn(text: str) -> list[float]:
        for key, vec in vectors.items():
            if key in text:
                return vec
        if default is not None:
            return default
        raise AssertionError(f"no stub vector registered matching: {text[:50]!r}")
    return embed_fn


class TestScoringStrategies(unittest.IsolatedAsyncioTestCase):
    async def test_score_whole_page_uses_full_content(self):
        embed_fn = make_stub_embed_fn({"relevant content": [1.0, 0.0]})
        score = await score_whole_page([1.0, 0.0], "relevant content here", embed_fn)
        self.assertAlmostEqual(score, 1.0)

    async def test_score_headings_falls_back_when_no_headings(self):
        embed_fn = make_stub_embed_fn({"no headings here": [1.0, 0.0]})
        score = await score_headings([1.0, 0.0], "no headings here, just prose", embed_fn)
        self.assertAlmostEqual(score, 1.0)

    async def test_score_headings_uses_headings_when_present(self):
        embed_fn = make_stub_embed_fn({"# Relevant Heading": [1.0, 0.0]}, default=[0.0, 1.0])
        md = "# Relevant Heading\n\nUnrelated prose body text goes here."
        score = await score_headings([1.0, 0.0], md, embed_fn)
        self.assertAlmostEqual(score, 1.0)  # matched the heading, not the irrelevant body

    async def test_score_max_chunk_takes_the_best_matching_chunk(self):
        # One chunk matches perfectly, the rest don't -- max should surface it
        # even though most of the page is irrelevant.
        text = ("irrelevant filler. " * 200) + "THE RELEVANT PART. " + ("more filler. " * 200)
        embed_fn = make_stub_embed_fn({"THE RELEVANT PART": [1.0, 0.0]}, default=[0.0, 1.0])
        score = await score_max_chunk([1.0, 0.0], text, embed_fn, chunk_size=500)
        self.assertAlmostEqual(score, 1.0)

    async def test_score_max_chunk_empty_content_scores_zero(self):
        embed_fn = make_stub_embed_fn({})
        score = await score_max_chunk([1.0, 0.0], "", embed_fn)
        self.assertEqual(score, 0.0)

    async def test_no_headings_fallback_truncates_long_content(self):
        # Regression: score_headings' no-headings fallback to
        # score_whole_page used to pass full untruncated content, which
        # score_whole_page then truncated with its OWN hardcoded 8000 --
        # a limit already proven to 500 a real embedding server on real
        # markdown. Both must now respect the same MAX_EMBED_CHARS.
        received_lengths = []

        async def embed_fn(text: str) -> list[float]:
            received_lengths.append(len(text))
            return [1.0, 0.0]

        long_content_no_headings = "no headings anywhere. " * 1000  # ~23000 chars
        await score_headings([1.0, 0.0], long_content_no_headings, embed_fn)
        self.assertEqual(len(received_lengths), 1)
        self.assertLessEqual(received_lengths[0], 4000)

    async def test_max_chunk_never_sends_a_chunk_over_the_embed_limit(self):
        received_lengths = []

        async def embed_fn(text: str) -> list[float]:
            received_lengths.append(len(text))
            return [1.0, 0.0]

        # A chunk_size larger than MAX_EMBED_CHARS must still get truncated
        # per-chunk before the embed call, not just rely on chunk_size
        # being small enough by convention.
        text = "word " * 2000  # ~10000 chars
        await score_max_chunk([1.0, 0.0], text, embed_fn, chunk_size=6000)
        self.assertTrue(received_lengths)
        self.assertTrue(all(n <= 4000 for n in received_lengths))


class TestMakeScoreFn(unittest.IsolatedAsyncioTestCase):
    async def test_no_intent_gate_is_off_scores_above_any_threshold(self):
        async def embed_fn(text):
            raise AssertionError("embed_fn must never be called when intent_embedding is None")

        score_fn = make_score_fn(None, embed_fn)
        score = await score_fn("https://x.com", "any content")
        self.assertGreaterEqual(score, 1.0)  # above any threshold in (0, 1]

    async def test_with_intent_delegates_to_score_headings(self):
        embed_fn = make_stub_embed_fn({"# Match": [1.0, 0.0]}, default=[0.0, 1.0])
        score_fn = make_score_fn([1.0, 0.0], embed_fn)
        score = await score_fn("https://x.com", "# Match\n\nbody text")
        self.assertAlmostEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
