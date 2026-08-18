"""Offline tests for dataset_report.py -- synthetic canonical records
built to exercise same_chunk / adjacent_chunk / other classification
directly, no LLM calls, no network."""
import unittest

from dataset_report import (
    build_dataset_report,
    classify_redundancy_cause,
    find_near_duplicates,
    pairs_per_page,
    redundancy_by_cause,
)


def _record(**overrides):
    base = {
        "question": "What is X?", "answer": "X is a thing that does Y and Z, in some detail.",
        "source_chunk": "chunk text about X", "chunk_index": 0, "source_url": "https://x.com/a",
        "section": "a", "page_title": "A", "generation_model": "deepseek-v4-flash",
        "extraction_strategy": "per_chunk", "timestamp": "2026-08-18T00:00:00+00:00",
        "crawl_date": "2026-08-18", "license_signal": None,
    }
    base.update(overrides)
    return base


class TestPairsPerPage(unittest.TestCase):
    def test_counts_grouped_by_url(self):
        records = [_record(source_url="https://x.com/a") for _ in range(3)]
        records += [_record(source_url="https://x.com/b") for _ in range(2)]
        counts = pairs_per_page(records)
        self.assertEqual(counts, {"https://x.com/a": 3, "https://x.com/b": 2})


class TestFindNearDuplicates(unittest.TestCase):
    def test_restricted_to_same_url_only(self):
        records = [
            _record(source_url="https://x.com/a", answer="the sky is blue and clear today"),
            _record(source_url="https://x.com/b", answer="the sky is blue and clear today"),
        ]
        dups = find_near_duplicates(records, "answer", threshold=0.9)
        self.assertEqual(dups, [])  # identical text, but different pages -- not counted

    def test_detects_near_duplicate_within_one_page(self):
        records = [
            _record(source_url="https://x.com/a", answer="Install the package using pip install foo"),
            _record(source_url="https://x.com/a", answer="Install the package by running pip install foo"),
        ]
        dups = find_near_duplicates(records, "answer", threshold=0.7)
        self.assertEqual(len(dups), 1)


class TestClassifyRedundancyCause(unittest.TestCase):
    def test_same_chunk_when_source_chunk_text_matches(self):
        records = [
            _record(source_chunk="same chunk", chunk_index=0),
            _record(source_chunk="same chunk", chunk_index=0),
        ]
        self.assertEqual(classify_redundancy_cause(records, 0, 1), "same_chunk")

    def test_adjacent_chunk_when_indices_are_consecutive(self):
        records = [
            _record(source_chunk="chunk A", chunk_index=3),
            _record(source_chunk="chunk B", chunk_index=4),
        ]
        self.assertEqual(classify_redundancy_cause(records, 0, 1), "adjacent_chunk")

    def test_other_when_same_page_but_not_adjacent(self):
        records = [
            _record(source_chunk="chunk A", chunk_index=0),
            _record(source_chunk="chunk Z", chunk_index=9),
        ]
        self.assertEqual(classify_redundancy_cause(records, 0, 1), "other")

    def test_other_when_different_pages(self):
        records = [
            _record(source_url="https://x.com/a", chunk_index=0),
            _record(source_url="https://x.com/b", chunk_index=0),
        ]
        self.assertEqual(classify_redundancy_cause(records, 0, 1), "other")


class TestRedundancyByCause(unittest.TestCase):
    def test_counts_each_cause_correctly(self):
        records = [
            # same_chunk pair: identical source_chunk, similar answers
            _record(source_chunk="sponsor list chunk", chunk_index=1,
                    answer="The gold sponsors are Foo, Bar, and Baz companies listed here"),
            _record(source_chunk="sponsor list chunk", chunk_index=1,
                    answer="Gold sponsors include Foo, Bar, and Baz as listed companies"),
            # adjacent_chunk pair: different chunks, consecutive index, overlapping answer content
            _record(source_chunk="chunk about docs UI part 1", chunk_index=5,
                    answer="Click Try it out to fill parameters and send the request"),
            _record(source_chunk="chunk about docs UI part 2", chunk_index=6,
                    answer="Click the Try it out button to fill parameters and send a request"),
        ]
        causes = redundancy_by_cause(records, field="answer", threshold=0.5)
        self.assertEqual(causes.get("same_chunk"), 1)
        self.assertEqual(causes.get("adjacent_chunk"), 1)
        self.assertNotIn("other", causes)


class TestBuildDatasetReport(unittest.TestCase):
    def test_report_contains_key_sections(self):
        records = [_record(question=f"Q{i}", chunk_index=i) for i in range(5)]
        report = build_dataset_report(records)
        self.assertIn("Total canonical pairs: 5", report)
        self.assertIn("Exact-duplicate questions", report)
        self.assertIn("Redundancy by cause", report)

    def test_scores_section_included_when_provided(self):
        records = [_record(source_url="https://x.com/a")]
        scores = [("https://x.com/a", 0.9, "done"), ("https://x.com/b", 0.1, "skipped_extract")]
        report = build_dataset_report(records, scores=scores)
        self.assertIn("Relevance scores vs. what was extracted", report)
        self.assertIn("pairs=1", report)
        self.assertIn("pairs=0", report)

    def test_empty_records_does_not_crash(self):
        report = build_dataset_report([])
        self.assertIn("Total canonical pairs: 0", report)


if __name__ == "__main__":
    unittest.main()
