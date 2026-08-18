"""Integration test for main.py's real fetch path (_make_fetch_fn) --
exercises actual crawl4ai HTML-cleaning through the real entry point,
not chrome_strip.py in isolation. This is precisely the class of bug
isolated testing missed: chrome_strip.py was fully built, tested, and
validated on its own (steps 4/9/11), but _make_fetch_fn never actually
called it (or its underlying mechanism) until step 8 Part D's live
crawl against docs.manim.community produced real nav-menu questions in
extraction output. See LESSONS_LEARNED.md -- the blind spot was
integration, not coverage.

Uses crawl4ai's `raw:` URL scheme to feed literal HTML through the real
AsyncWebCrawler.arun() call -- no network fetch, but genuinely exercises
CrawlerRunConfig's excluded_tags/excluded_selector and the real
markdown-generation pipeline, not a stand-in for it.
"""
import unittest

from crawl4ai import AsyncWebCrawler

from main import _make_fetch_fn

FIXTURE_HTML = """
<html><body>
<nav>Site navigation: Home | Docs | About | Reference</nav>
<aside class="sidebar">
<ul><li><a href="/a">Section A</a></li><li><a href="/b">Section B</a></li></ul>
</aside>
<main>
<h1>Real API Reference</h1>
<p>This function does something real, worth extracting.</p>
<button title="Copy to clipboard">Copy to clipboard</button>
</main>
<footer>Copyright 2026 Example Corp. All rights reserved.</footer>
</body></html>
"""


class TestRealFetchFnStripsChrome(unittest.IsolatedAsyncioTestCase):
    async def test_nav_aside_footer_and_button_chrome_absent_from_real_fetch_output(self):
        async with AsyncWebCrawler() as crawler:
            fetch_fn = _make_fetch_fn(crawler)
            markdown, _raw_hrefs = await fetch_fn("raw:" + FIXTURE_HTML)

        self.assertNotIn("Site navigation", markdown)
        self.assertNotIn("Section A", markdown)
        self.assertNotIn("Copyright 2026", markdown)
        self.assertNotIn("Copy to clipboard", markdown)
        self.assertIn("Real API Reference", markdown)
        self.assertIn("something real, worth extracting", markdown)


if __name__ == "__main__":
    unittest.main()
