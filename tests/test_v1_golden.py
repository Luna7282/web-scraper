"""v1 and legacy canonical rows are BYTE-identical to web-scraper main 1112ad7
(before Brief 3B). tests/golden/make_rows.py runs a deterministic headless run
(fake crawler / fetch / extractor, frozen clock, fixed version and run id) in a
fresh interpreter against the current code; the golden files were produced by
the same script from a `git archive 1112ad7` export. No network.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "golden"


class TestV1AndLegacyRowsByteIdentical(unittest.TestCase):
    def rows(self, mode):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "canonical.jsonl"
            subprocess.run([sys.executable, str(GOLDEN / "make_rows.py"), mode, str(out)], cwd=REPO, check=True,
                           capture_output=True, timeout=120, env={**os.environ, "ANONYMIZED_TELEMETRY": "False"})
            return out.read_bytes()

    def test_v1_rows_are_byte_identical_to_pre_3b_main(self):
        self.assertEqual(self.rows("v1"), (GOLDEN / "canonical_v1_main_1112ad7.jsonl").read_bytes())

    def test_legacy_rows_are_byte_identical_to_pre_3b_main(self):
        self.assertEqual(self.rows("legacy"), (GOLDEN / "canonical_legacy_main_1112ad7.jsonl").read_bytes())


if __name__ == "__main__":
    unittest.main()
