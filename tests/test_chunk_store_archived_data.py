"""Verifies chunk_id() against the actual, known cross-page duplicate
chunks from the pre-rebuild audit (archive/pre-rebuild/chroma_db) --
asserting the collapse on real data rather than hoping a synthetic
example generalizes. Offline: reads the archived sqlite3 file directly,
no chromadb/embedding involved.

Ground truth this checks against (verified once by direct query, not
re-derived here): the exact text "The name for the Python project is
_manimations_ , which you can change to anything you like.\\n\\n```\\nuv
init manimations\\ncd manimations\\nuv add manim" appears byte-identical
as its own stored chunk on all four installation pages (linux, macos,
windows, uv) in the archived chroma_db -- this is exactly the
cross-page-duplicate case chunk_id()'s hash-text-alone design (ROADMAP #6)
exists to collapse.
"""
import sqlite3
import unittest
from pathlib import Path

from chunk_store import chunk_id, normalize_chunk_text

ARCHIVED_DB = Path(__file__).parent.parent / "archive" / "pre-rebuild" / "chroma_db" / "chroma.sqlite3"

KNOWN_CROSS_PAGE_DUPLICATE_TEXT = (
    "The name for the Python project is _manimations_ , which you can "
    "change to anything you like.\n\n```\nuv init manimations\ncd manimations\nuv add manim"
)
EXPECTED_SOURCE_PAGES = {
    "https://docs.manim.community/en/stable/installation/linux.html",
    "https://docs.manim.community/en/stable/installation/macos.html",
    "https://docs.manim.community/en/stable/installation/windows.html",
    "https://docs.manim.community/en/stable/installation/uv.html",
}


@unittest.skipUnless(ARCHIVED_DB.exists(), "archived pre-rebuild chroma_db not present")
class TestChunkIdAgainstArchivedDuplicates(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(str(ARCHIVED_DB))

    def tearDown(self):
        self.con.close()

    def _sources_for_document(self, document_text: str) -> set[str]:
        cur = self.con.execute(
            """
            SELECT em1.string_value FROM embedding_metadata em1
            JOIN embedding_metadata em2 ON em1.id = em2.id
            WHERE em1.key = 'source' AND em2.key = 'chroma:document' AND em2.string_value = ?
            """,
            (document_text,),
        )
        return {row[0] for row in cur.fetchall()}

    def test_known_duplicate_text_confirmed_present_on_all_four_pages(self):
        # Sanity-check the ground truth itself before asserting anything
        # about chunk_id -- if this fails, the archived data changed or
        # the reference text above is wrong, not chunk_id.
        sources = self._sources_for_document(KNOWN_CROSS_PAGE_DUPLICATE_TEXT)
        self.assertEqual(sources, EXPECTED_SOURCE_PAGES)

    def test_chunk_id_collapses_the_known_cross_page_duplicate(self):
        # The actual point of this module: one hash for content that
        # already exists identically on 4 different real pages.
        ids = {chunk_id(KNOWN_CROSS_PAGE_DUPLICATE_TEXT) for _ in EXPECTED_SOURCE_PAGES}
        self.assertEqual(len(ids), 1)

    def test_pulling_the_same_text_fresh_from_each_source_still_collides(self):
        # Re-fetch the document text keyed by each real source URL
        # independently (not just reusing the same Python string four
        # times), to rule out a trivial "of course it's equal, it's the
        # same variable" false confidence.
        cur = self.con.execute(
            """
            SELECT DISTINCT em1.string_value as source, em2.string_value as document
            FROM embedding_metadata em1
            JOIN embedding_metadata em2 ON em1.id = em2.id
            WHERE em1.key = 'source' AND em2.key = 'chroma:document'
            AND em1.string_value IN (
                'https://docs.manim.community/en/stable/installation/linux.html',
                'https://docs.manim.community/en/stable/installation/macos.html',
                'https://docs.manim.community/en/stable/installation/windows.html',
                'https://docs.manim.community/en/stable/installation/uv.html'
            )
            AND em2.string_value LIKE '%manimations%uv init manimations%'
            """
        )
        rows = cur.fetchall()
        self.assertEqual(len(rows), 4, "expected exactly 4 real (source, document) rows for this text")
        ids = {chunk_id(document) for _source, document in rows}
        self.assertEqual(len(ids), 1, f"expected all 4 real pages' copies to collide to one id, got {len(ids)}")

    def test_all_49_real_content_duplicate_pairs_from_the_step4_dump_still_collide_pairwise(self):
        # Broader check across every (source, document) pair with count>1
        # from the original step-4 audit dump -- not just the one
        # hand-picked "manimations" example. Groups by normalized text and
        # confirms every group that spans >1 distinct source URL produces
        # exactly one chunk_id.
        cur = self.con.execute(
            """
            SELECT em2.string_value as document, em1.string_value as source
            FROM embedding_metadata em1
            JOIN embedding_metadata em2 ON em1.id = em2.id
            WHERE em1.key = 'source' AND em2.key = 'chroma:document'
            """
        )
        by_normalized: dict[str, set[str]] = {}
        by_normalized_ids: dict[str, set[str]] = {}
        for document, source in cur.fetchall():
            key = normalize_chunk_text(document)
            by_normalized.setdefault(key, set()).add(source)
            by_normalized_ids.setdefault(key, set()).add(chunk_id(document))

        cross_page_groups = {k: v for k, v in by_normalized.items() if len(v) > 1}
        self.assertGreater(len(cross_page_groups), 0, "expected at least some real cross-page duplicates")
        for key in cross_page_groups:
            self.assertEqual(
                len(by_normalized_ids[key]), 1,
                f"cross-page duplicate group did not collapse to one chunk_id: {key[:80]!r}",
            )


if __name__ == "__main__":
    unittest.main()
