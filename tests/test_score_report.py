"""Offline tests for score_report.py's pure formatting -- fake data, no
frontier DB, no network."""
import unittest

from score_report import format_score_report


class TestFormatScoreReport(unittest.TestCase):
    def test_empty_scores_reports_nothing_found(self):
        report = format_score_report([])
        self.assertIn("No scored pages", report)

    def test_reports_count_and_stats(self):
        scores = [
            ("https://x.com/a", 0.9, "done"),
            ("https://x.com/b", 0.3, "skipped_extract"),
            ("https://x.com/c", 0.5, "done"),
        ]
        report = format_score_report(scores)
        self.assertIn("Scored pages: 3", report)
        self.assertIn("max=0.9000", report)
        self.assertIn("min=0.3000", report)

    def test_every_page_listed_with_status(self):
        scores = [("https://x.com/a", 0.7, "done")]
        report = format_score_report(scores)
        self.assertIn("0.7000", report)
        self.assertIn("[done]", report)
        self.assertIn("https://x.com/a", report)

    def test_threshold_skip_counts_correct(self):
        scores = [
            ("https://x.com/a", 0.9, "done"),
            ("https://x.com/b", 0.5, "done"),
            ("https://x.com/c", 0.1, "done"),
        ]
        report = format_score_report(scores)
        # threshold=0.5: only the 0.1 page scores below it -> 1 skipped
        self.assertIn("threshold=0.5:  1/3 pages would be skipped", report)
        # threshold=0.9: both 0.5 and 0.1 score below it -> 2 skipped
        self.assertIn("threshold=0.9:  2/3 pages would be skipped", report)
        # threshold=0.1: nothing scores below 0.1 -> 0 skipped
        self.assertIn("threshold=0.1:  0/3 pages would be skipped", report)

    def test_all_nine_candidate_thresholds_present(self):
        scores = [("https://x.com/a", 0.5, "done")]
        report = format_score_report(scores)
        for t in ["0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9"]:
            self.assertIn(f"threshold={t}:", report)


if __name__ == "__main__":
    unittest.main()
