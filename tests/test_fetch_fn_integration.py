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

import config
from content.extraction_units import select_extraction_units
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
<p>See <a href="/reference/Widget.html#Widget" title="reference.Widget">Widget</a> for the related class.</p>
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
        self.assertIn("Widget", markdown)
        self.assertNotIn("reference/Widget.html", markdown)


# Reproduces LESSONS_LEARNED.md #32's two root causes, in the same shape
# real Furo-themed pages ship them: an invisible icon-sprite <title>
# block with no landmark tag or excluded-tag match to catch it, and bare
# utility links (skip-to-content, back-to-top, edit-this-page) sitting
# directly under <body>, not inside <nav>/<header>/<aside>.
FURO_STYLE_FIXTURE_HTML = """
<html><body>
<svg style="display: none;">
  <symbol id="svg-sun"><title>Light mode</title></symbol>
  <symbol id="svg-menu"><title>Menu</title></symbol>
</svg>
<a class="skip-to-content muted-link" href="#main-content">Skip to content</a>
<main id="main-content">
<h1>Real API Reference</h1>
<p>This function does something real, worth extracting, and long enough on its own to form a full extraction chunk without needing to be padded out with filler content just to reach a reasonable chunk size for this test.</p>
<a href="#" class="back-to-top muted-link"><span>Back to top</span></a>
<div class="edit-this-page">
  <a class="muted-link" href="https://github.com/example/repo/edit/main/page.rst" title="Edit this page">
    <span class="visually-hidden">Edit this page</span>
  </a>
</div>
</main>
</body></html>
"""


class TestRealFetchFnStripsChromeFromChunkedOutput(unittest.IsolatedAsyncioTestCase):
    async def test_furo_style_chrome_absent_from_every_extraction_chunk(self):
        async with AsyncWebCrawler() as crawler:
            fetch_fn = _make_fetch_fn(crawler)
            markdown, _raw_hrefs = await fetch_fn("raw:" + FURO_STYLE_FIXTURE_HTML)

        chunks = await select_extraction_units(markdown, config.EXTRACTION_STRATEGY)
        self.assertTrue(chunks, "fixture should produce at least one extraction chunk")

        chrome_phrases = ["Light mode", "Menu", "Skip to content", "Back to top", "Edit this page"]
        for chunk in chunks:
            for phrase in chrome_phrases:
                self.assertNotIn(phrase, chunk, f"{phrase!r} leaked into a source_chunk: {chunk!r}")
        self.assertIn("something real, worth extracting", "".join(chunks))


if __name__ == "__main__":
    unittest.main()
