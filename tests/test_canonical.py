"""Offline tests for canonical.py -- pure functions, no I/O."""
import unittest

from storage.canonical import build_canonical_record, detect_license_signal, extract_page_title


class TestBuildCanonicalRecord(unittest.TestCase):
    def test_has_all_expected_fields(self):
        record = build_canonical_record(
            question="Q", answer="A", source_chunk="chunk", chunk_index=0, source_url="https://x.com/a",
            section="tutorial", page_title="Title", generation_model="deepseek-v4-flash",
            extraction_strategy="per_chunk", timestamp="2026-08-18T00:00:00+00:00",
            crawl_date="2026-08-18", license_signal=None,
        )
        self.assertEqual(record["question"], "Q")
        self.assertEqual(record["answer"], "A")
        self.assertEqual(record["source_chunk"], "chunk")
        self.assertEqual(record["chunk_index"], 0)
        self.assertIsNone(record["license_signal"])


class TestExtractPageTitle(unittest.TestCase):
    def test_plain_h1(self):
        self.assertEqual(extract_page_title("# FastAPI\n\nsome text"), "FastAPI")

    def test_h1_with_trailing_anchor_link_markup_stripped(self):
        md = '# FastAPI[¶](https://fastapi.tiangolo.com/#fastapi "Permanent link")\n\nbody'
        self.assertEqual(extract_page_title(md), "FastAPI")

    def test_no_heading_returns_none(self):
        self.assertIsNone(extract_page_title("just some prose, no headings at all"))

    def test_h2_alone_is_not_a_title(self):
        self.assertIsNone(extract_page_title("## Not a title\n\nbody"))


class TestDetectLicenseSignal(unittest.TestCase):
    def test_mit_license_detected(self):
        self.assertEqual(detect_license_signal("Released under the MIT License."), "MIT License")

    def test_copyright_line_detected(self):
        signal = detect_license_signal("Page footer text. © 2026 Example Corp, all trademarks apply.")
        self.assertIsNotNone(signal)
        self.assertTrue(signal.startswith("©"))

    def test_no_signal_returns_none(self):
        self.assertIsNone(detect_license_signal("just some regular page content"))


if __name__ == "__main__":
    unittest.main()
