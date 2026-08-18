"""Unit tests for chrome_strip.py. Offline -- synthetic HTML, no network,
no dependency on fixture content (fixture-based before/after reporting is
separate, see tests/report_chrome_strip.py)."""
import unittest

from content.chrome_strip import clean_html, normalize_link_text, strip_text_patterns, strip_chrome

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


class TestNormalizeLinkText(unittest.TestCase):
    def test_informative_link_text_kept_url_dropped(self):
        md = 'Let’s define a new [`Scene`](https://docs.manim.community/en/stable/reference/manim.scene.scene.Scene.html#manim.scene.scene.Scene "manim.scene.scene.Scene") called `Shapes`.'
        result = normalize_link_text(md)
        self.assertIn("`Scene`", result)
        self.assertNotIn("docs.manim.community", result)
        self.assertNotIn("(", result)

    def test_heading_anchor_pilcrow_dropped_entirely(self):
        md = '### Placing mobjects[¶](https://docs.manim.community/en/stable/tutorials/building_blocks.html#placing-mobjects "Link to this heading")'
        result = normalize_link_text(md)
        self.assertEqual(result, "### Placing mobjects")

    def test_reference_table_row_reduced_to_symbol_names(self):
        md = '| [`biolinum`](https://docs.manim.community/en/stable/reference/manim.utils.tex_templates.TexFontTemplates.html#manim.utils.tex_templates.TexFontTemplates.biolinum "manim.utils.tex_templates.TexFontTemplates.biolinum")  | Biolinum  |'
        result = normalize_link_text(md)
        self.assertEqual(result, "| `biolinum`  | Biolinum  |")

    def test_generic_click_here_text_dropped(self):
        md = 'See the docs [click here](https://example.com/docs) for details.'
        result = normalize_link_text(md)
        self.assertEqual(result, "See the docs  for details.")

    def test_custom_generic_text_overrides_default(self):
        md = "[widget](https://example.com/widget) info"
        result = normalize_link_text(md, generic_text=["widget"])
        self.assertEqual(result, " info")

    def test_image_syntax_left_untouched(self):
        md = "![alt text](https://example.com/pic.png)"
        result = normalize_link_text(md)
        self.assertEqual(result, md)

    def test_plain_text_without_links_unchanged(self):
        md = "Just a normal paragraph with no links at all."
        result = normalize_link_text(md)
        self.assertEqual(result, md)


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

    def test_full_pipeline_normalizes_link_syntax(self):
        html = '<html><body><main><p>See <a href="https://example.com/api#Widget" title="example.com api Widget">Widget</a> for details.</p></main></body></html>'
        result = strip_chrome(html, BASE)
        self.assertIn("Widget", result)
        self.assertNotIn("example.com", result)


if __name__ == "__main__":
    unittest.main()
