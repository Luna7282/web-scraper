"""Parser-layer chrome stripping.

Three layers, applied in order:

1. Structural: exclude by semantic HTML tag and ARIA role, via crawl4ai's
   own HTML-cleaning stage (LXMLWebScrapingStrategy's excluded_tags /
   excluded_selector). This is the primary mechanism -- e.g. a genuine
   `<button title="Copy to clipboard">` (confirmed present on
   fastapi.tiangolo.com's rendered HTML) is caught by excluding the
   `button` tag itself, no site-specific class needed.

2. Link-text normalization: `[text](url "title")` collapses to its
   visible text (see normalize_link_text below) -- markdown link syntax
   measured at 46.7% of real chunk characters (LESSONS_LEARNED.md #31),
   the largest single content-quality finding in this project.

3. Text-pattern fallback: some UI affordances leak through as bare text
   even after structural exclusion (a theme's tooltip text sitting in a
   plain <span> with no distinguishing tag or role, for instance). The
   patterns are theme-specific by nature -- every docs theme phrases its
   own copy-button/interactive-preview text differently -- so they live in
   config (DEFAULT_TEXT_PATTERNS below, overridable), never hardcoded
   inside the matching logic itself.

Nothing here branches on a domain name or theme name. Same functions run
against every site; only the *data* (excluded_tags/selector, generic link
text, text patterns) is meant to be extended, and that extension point is
the constants below / a caller-supplied override, not a code path.
"""
from __future__ import annotations

import re

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
# [hidden] (the native HTML attribute) and the two style*= clauses (the
# common no-space/spaced CSS serializations of "display:none;") are the
# same signal by a different route: an element the page itself marks as
# never rendered. Structural tag/role exclusion doesn't evaluate CSS, so
# without this an invisible icon-sprite <title> (real example: Furo's
# `<svg style="display: none;">` accessibility-label block, see
# LESSONS_LEARNED.md #32) gets extracted as if it were visible content.
DEFAULT_EXCLUDED_SELECTOR = (
    '[role="navigation"], [role="banner"], [role="contentinfo"], '
    '[role="complementary"], [role="button"], [aria-hidden="true"], '
    '[hidden], [style*="display:none"], [style*="display: none"]'
)

# Fallback for UI-affordance text that survives structural exclusion.
# Theme-specific by nature -- extend per-theme via the patterns param,
# don't hardcode a new phrase into strip_text_patterns() itself. The
# skip-to-content/back-to-top/view-or-edit-this-page entries are Furo's
# copy (LESSONS_LEARNED.md #32's root cause 2 -- bare utility links in
# plain div/label wrappers with no landmark tag or ARIA role for
# structural exclusion to match), but the phrases themselves are common
# across many doc themes, not a Furo-only string.
DEFAULT_TEXT_PATTERNS = [
    "copy to clipboard", "copied to clipboard", "copied!", "make interactive",
    "skip to content", "skip to main content", "skip navigation",
    "back to top", "scroll to top", "view this page", "edit this page",
    "toggle navigation", "toggle table of contents", "toggle theme",
]

# Link visible-text that carries no identifying information on its own --
# heading-anchor pilcrows, generic "click here" style copy, and the same
# utility-link phrases as DEFAULT_TEXT_PATTERNS above (these arrive as
# markdown link text before strip_text_patterns ever sees them, since
# they're <a> elements, not bare text). Generic across themes, not
# site-specific -- extend via the generic_text param, don't hardcode a
# new phrase into normalize_link_text() itself. A link matching one of
# these is dropped entirely (text and URL both), not just unwrapped,
# since neither side is worth keeping.
DEFAULT_GENERIC_LINK_TEXT = {
    "here", "this", "link", "click here", "this page", "this link",
    "read more", "more", "learn more", "source", "top", "¶",
    "skip to content", "skip to main content", "skip navigation",
    "back to top", "scroll to top", "view this page", "edit this page",
}

# Text group allows one level of nested [...] -- Sphinx's auto-generated
# "[source]" links render as a bracketed label wrapped in markdown link
# syntax (`[[source]](url)`), and a text group excluding all `]` can't
# match past the inner one. Real example: confirmed present, unmatched
# without this, on tests/fixtures/docs_manim_reference.html.
_LINK_RE = re.compile(r'(!?)\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\([^)\s]+(?:\s+"[^"]*")?\)')


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


def normalize_link_text(markdown: str, generic_text=None) -> str:
    """Reduces `[text](url "title")` to its visible text. Measured at
    46.7% of real chunk characters being link syntax, only 2.9% visible
    text -- see LESSONS_LEARNED.md #31, the largest single content-quality
    finding in this project.

    URLs are dropped, not preserved in a reference list: every link
    surveyed in the real corpus either duplicated its own visible text
    (a symbol name linking to its own definition) or pointed at a
    same-page anchor -- the target added no fact the text didn't already
    carry, and this project's Q&A/RAG pipeline never consumes a link
    target as data (source_url is already a separate canonical-record
    field). Images (`![...]`) are left untouched -- alt text without the
    src isn't independently useful, so there's nothing to unwrap.

    A link whose visible text is empty, has no alphanumeric content, or
    matches DEFAULT_GENERIC_LINK_TEXT (heading-anchor pilcrows, "click
    here" style copy) is dropped entirely -- neither the text nor the URL
    is worth keeping, so unwrapping it would just leave dead punctuation
    or noise behind."""
    generic = {g.strip().lower() for g in (generic_text if generic_text is not None else DEFAULT_GENERIC_LINK_TEXT)}

    def repl(match: re.Match) -> str:
        bang, text = match.group(1), match.group(2)
        if bang:
            return match.group(0)
        stripped = text.strip().strip("`")
        if not stripped or not re.search(r"[A-Za-z0-9]", stripped) or stripped.lower() in generic:
            return ""
        return text

    return _LINK_RE.sub(repl, markdown)


def strip_chrome(html: str, url: str, excluded_tags=None, excluded_selector=None, text_patterns=None, generic_link_text=None) -> str:
    """Full pipeline: structural strip -> markdown -> link-text
    normalization -> text-pattern fallback. Offline, no network call --
    mirrors what CrawlerRunConfig (excluded_tags/excluded_selector) does
    for the real pipeline, so this can be developed and tested against
    cached HTML fixtures."""
    cleaned_html = clean_html(html, url, excluded_tags, excluded_selector)
    markdown = DefaultMarkdownGenerator().generate_markdown(input_html=cleaned_html, base_url=url).raw_markdown
    markdown = normalize_link_text(markdown, generic_link_text)
    return strip_text_patterns(markdown, text_patterns)
