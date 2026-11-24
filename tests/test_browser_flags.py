"""ROADMAP #70 (HIGH): crawl4ai's default --disable-blink-features=AutomationControlled
(hides navigator.webdriver) is removed from every Chromium launch. Nothing is
launched here: the arg builders are called directly."""

import unittest

import tests.test_000_network_guard  # noqa: F401  (the fast suite's network guard)

FLAG = "--disable-blink-features=AutomationControlled"


class TestNoAutomationHiding(unittest.TestCase):
    def test_crawl4ai_adds_it_by_default(self):
        """Pins why the fix exists: crawl4ai's own builders add the flag (if an
        upgrade stops doing so, this test says the wrapper can go)."""
        from crawl4ai import BrowserConfig
        from crawl4ai import browser_manager as bm

        from crawl import browser_flags
        browser_flags.install()
        original = bm.BrowserManager._build_browser_args.original
        self.assertIn(FLAG, original(bm.BrowserManager(BrowserConfig()))["args"])
        self.assertIn(FLAG, bm.ManagedBrowser.build_browser_flags.original(BrowserConfig()))

    def test_both_launch_paths_are_clean_after_install(self):
        from crawl4ai import BrowserConfig
        from crawl4ai import browser_manager as bm

        import main  # noqa: F401  (main.py installs the fix at import)
        args = bm.BrowserManager(BrowserConfig())._build_browser_args()["args"]
        self.assertNotIn(FLAG, args)
        self.assertIn("--no-first-run", args)                       # the rest of crawl4ai's args stay
        flags = bm.ManagedBrowser.build_browser_flags(BrowserConfig())
        self.assertNotIn(FLAG, flags)
        self.assertIn("--no-first-run", flags)

    def test_install_is_idempotent(self):
        from crawl4ai import browser_manager as bm

        from crawl import browser_flags
        browser_flags.install()
        first = bm.BrowserManager._build_browser_args
        browser_flags.install()
        self.assertIs(bm.BrowserManager._build_browser_args, first)


if __name__ == "__main__":
    unittest.main()
