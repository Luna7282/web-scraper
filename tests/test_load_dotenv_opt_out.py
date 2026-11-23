"""SCRAPER_LOAD_DOTENV=0 skips web-scraper/.env; anything else loads it as before.

Each case imports config in a fresh interpreter, with dotenv's file search
pointed at a temporary .env holding only a marker variable, and reports only
whether the marker arrived. The real web-scraper/.env is never read.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MARKER = "SCRAPER_DOTENV_TEST_MARKER"


def marker_loaded(load_dotenv_value):
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / ".env").write_text(f"{MARKER}=from-dotenv\n")
        env = {k: v for k, v in os.environ.items() if k not in (MARKER, "SCRAPER_LOAD_DOTENV")}
        if load_dotenv_value is not None:
            env["SCRAPER_LOAD_DOTENV"] = load_dotenv_value
        env["PYTHONPATH"] = str(REPO)
        code = ("import dotenv, os; dotenv.main.find_dotenv = lambda *a, **k: os.path.join(os.getcwd(), '.env'); "
                "import config; print(os.environ.get('" + MARKER + "') == 'from-dotenv')")
        out = subprocess.run([sys.executable, "-c", code], cwd=d, env=env, capture_output=True, text=True,
                             check=True).stdout.strip()
        return out == "True"


class TestLoadDotenvOptOut(unittest.TestCase):
    def test_zero_skips_the_dotenv_file(self):
        self.assertFalse(marker_loaded("0"))
        self.assertFalse(marker_loaded(" 0 "))

    def test_default_and_other_values_load_it_as_before(self):
        self.assertTrue(marker_loaded(None))
        self.assertTrue(marker_loaded("1"))
        self.assertTrue(marker_loaded(""))


if __name__ == "__main__":
    unittest.main()
