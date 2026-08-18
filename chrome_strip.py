"""Parser-layer chrome stripping.

Two layers, applied in order:

1. Structural: exclude by semantic HTML tag and ARIA role, via crawl4ai's
   own HTML-cleaning stage (LXMLWebScrapingStrategy's excluded_tags /
   excluded_selector). This is the primary mechanism -- e.g. a genuine
   `<button title="Copy to clipboard">` (confirmed present on
   fastapi.tiangolo.com's rendered HTML) is caught by excluding the
   `button` tag itself, no site-specific class needed.

2. Text-pattern fallback: some UI affordances leak through as bare text
   even after structural exclusion (a theme's tooltip text sitting in a
   plain <span> with no distinguishing tag or role, for instance). The
   patterns are theme-specific by nature -- every docs theme phrases its
   own copy-button/interactive-preview text differently -- so they live in
   config (DEFAULT_TEXT_PATTERNS below, overridable), never hardcoded
   inside the matching logic itself.

Nothing here branches on a domain name or theme name. Same two functions
run against every site; only the *data* (excluded_tags/selector, text
patterns) is meant to be extended, and that extension point is the
constants below / a caller-supplied override, not a code path.
"""
from __future__ import annotations

from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

# Semantic landmark tags -- nav/header/footer/aside chrome (category B).
# button -- inline UI affordances living inside content, not layout
# chrome, but still not page content (category A). script/style/noscript
# -- never content, standard hygiene.
DEFAULT_EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "button", "script", "style", "noscript"]

# ARIA-role equivalents, for sites that use styled <div>s instead of
# semantic tags. role="button" catches non-<button> elements built to
# behave like one (<span role="button">, <a role="button">).
# aria-hidden="true" marks an element as decorative/non-content by the
# page's own accessibility markup -- a generic, theme-agnostic signal.
DEFAULT_EXCLUDED_SELECTOR = (
    '[role="navigation"], [role="banner"], [role="contentinfo"], '
    '[role="complementary"], [role="button"], [aria-hidden="true"]'
)

# Fallback for UI-affordance text that survives structural exclusion.
# Theme-specific by nature -- extend per-theme via the patterns param,
# don't hardcode a new phrase into strip_text_patterns() itself.
DEFAULT_TEXT_PATTERNS = ["copy to clipboard", "copied to clipboard", "copied!", "make interactive"]


def clean_html(html: str, url: str, excluded_tags=None, excluded_selector=None) -> str:
    """Structural stripping (layer 1). Offline -- takes an HTML string,
    makes no network call. `url` is only used for relative-link
    resolution inside crawl4ai's scraper, not fetched."""
    scraper = LXMLWebScrapingStrategy()
    result = scraper.scrap(
        url=url,
        html=html,
        excluded_tags=excluded_tags if excluded_tags is not None else DEFAULT_EXCLUDED_TAGS,
        excluded_selector=excluded_selector if excluded_selector is not None else DEFAULT_EXCLUDED_SELECTOR,
    )
    return result.cleaned_html


def strip_text_patterns(markdown: str, patterns=None) -> str:
    """Text-pattern fallback (layer 2). Drops a line if, once stripped of
    a leading code-fence marker and surrounding whitespace, its entire
    remaining content case-insensitively matches one of the patterns --
    not "contains the pattern anywhere in a longer sentence", to avoid
    deleting real content that happens to mention e.g. "copy to
    clipboard" as an actual sentence fragment rather than button text."""
    patterns = patterns if patterns is not None else DEFAULT_TEXT_PATTERNS
    normalized_patterns = {p.strip().lower() for p in patterns}

    out_lines = []
    for line in markdown.split("\n"):
        candidate = line.strip()
        if candidate.startswith("```"):
            candidate = candidate[3:].strip()
        if candidate.lower() in normalized_patterns:
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def strip_chrome(html: str, url: str, excluded_tags=None, excluded_selector=None, text_patterns=None) -> str:
    """Full pipeline: structural strip -> markdown -> text-pattern
    fallback. Offline, no network call -- mirrors what CrawlerRunConfig
    (excluded_tags/excluded_selector) does for the real pipeline, so this
    can be developed and tested against cached HTML fixtures."""
    cleaned_html = clean_html(html, url, excluded_tags, excluded_selector)
    markdown = DefaultMarkdownGenerator().generate_markdown(input_html=cleaned_html, base_url=url).raw_markdown
    return strip_text_patterns(markdown, text_patterns)
