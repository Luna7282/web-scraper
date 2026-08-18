"""Unit tests for chrome_strip.py. Offline -- synthetic HTML, no network,
no dependency on fixture content (fixture-based before/after reporting is
separate, see tests/report_chrome_strip.py)."""
import unittest

from content.chrome_strip import clean_html, strip_text_patterns, strip_chrome

BASE = "https://example.com/page"


class TestCleanHtml(unittest.TestCase):
    def test_nav_removed(self):
        html = "<html><body><nav>Home About Contact</nav><main>Real content</main></body></html>"
        cleaned = clean_html(html, BASE)
        self.assertNotIn("Home About Contact", cleaned)
        self.assertIn("Real content", cleaned)

    def test_header_footer_aside_removed(self):
        html = (
            "<html><body>"
            "<header>Site Header</header>"
            "<aside>Sidebar links</aside>"
            "<main>Real content</main>"
            "<footer>Copyright 2026</footer>"
            "</body></html>"
        )
        cleaned = clean_html(html, BASE)
        for junk in ("Site Header", "Sidebar links", "Copyright 2026"):
            self.assertNotIn(junk, cleaned)
        self.assertIn("Real content", cleaned)

    def test_button_removed(self):
        html = '<html><body><pre><code>real code</code></pre><button title="Copy to clipboard"></button></body></html>'
        cleaned = clean_html(html, BASE)
        self.assertNotIn("Copy to clipboard", cleaned)
        self.assertIn("real code", cleaned)

    def test_role_navigation_div_removed(self):
        html = '<html><body><div role="navigation">Menu items here</div><main>Real content</main></body></html>'
        cleaned = clean_html(html, BASE)
        self.assertNotIn("Menu items here", cleaned)
        self.assertIn("Real content", cleaned)

    def test_role_button_span_removed(self):
        html = '<html><body><span role="button">Click me</span><main>Real content</main></body></html>'
        cleaned = clean_html(html, BASE)
        self.assertNotIn("Click me", cleaned)
        self.assertIn("Real content", cleaned)

    def test_aria_hidden_removed(self):
        html = '<html><body><span aria-hidden="true">decorative icon</span><main>Real content</main></body></html>'
        cleaned = clean_html(html, BASE)
        self.assertNotIn("decorative icon", cleaned)
        self.assertIn("Real content", cleaned)

    def test_main_content_survives_with_no_exclusions_matched(self):
        html = "<html><body><main><p>Just a normal paragraph.</p></main></body></html>"
        cleaned = clean_html(html, BASE)
        self.assertIn("Just a normal paragraph.", cleaned)


class TestStripTextPatterns(unittest.TestCase):
    def test_exact_match_line_removed(self):
        md = "Some real content.\nCopy to clipboard\nMore real content."
        result = strip_text_patterns(md)
        self.assertNotIn("Copy to clipboard", result)
        self.assertIn("Some real content.", result)
        self.assertIn("More real content.", result)

    def test_code_fence_prefixed_match_removed(self):
        md = "```\nCopy to clipboard"
        result = strip_text_patterns(md)
        self.assertEqual(result.strip(), "```")

    def test_case_insensitive_match(self):
        md = "COPY TO CLIPBOARD"
        result = strip_text_patterns(md)
        self.assertEqual(result, "")

    def test_sentence_mentioning_phrase_is_not_stripped(self):
        # Real content that happens to *discuss* clipboard copying in a
        # full sentence must survive -- only a bare matching line is chrome.
        md = "This guide explains how the copy to clipboard button works internally."
        result = strip_text_patterns(md)
        self.assertEqual(result, md)

    def test_custom_pattern_list_overrides_default(self):
        md = "Run in browser\nReal content"
        result = strip_text_patterns(md, patterns=["run in browser"])
        self.assertNotIn("Run in browser", result)
        self.assertIn("Real content", result)


class TestStripChromeEndToEnd(unittest.TestCase):
    def test_full_pipeline_removes_nav_and_button_text(self):
        html = (
            "<html><body>"
            "<nav>Home About</nav>"
            "<main><pre><code>x = 1</code></pre></main>"
            '<button title="Copy to clipboard"></button>'
            "</body></html>"
        )
        result = strip_chrome(html, BASE)
        self.assertNotIn("Home About", result)
        self.assertNotIn("Copy to clipboard", result)
        self.assertIn("x = 1", result)


if __name__ == "__main__":
    unittest.main()
