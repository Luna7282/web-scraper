"""sitemap.xml parsing -- pure function, no I/O, mirrors crawl/scope.py's
"parsing/decision logic stays separate from fetching" discipline.
RobotsCache already discovers a host's sitemap_urls (crawl/robots_cache.py)
but never fetches/parses their content; this is what turns that URL list
into actual page URLs for map mode's "seed from sitemap.xml instead of
fetching" option.

A sitemap.xml is either a urlset (page URLs directly) or a sitemapindex
(pointers to other sitemaps, which themselves need fetching) -- these are
returned separately so the caller decides whether/how to follow an index,
rather than this function silently recursing (it can't: it has no fetcher).
"""
from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree

# Sitemap XML is namespaced (typically the sitemaps.org schema) but real
# sitemaps vary the exact namespace URI (versioned, vendor variants) --
# matching on the local tag name only (stripping any `{...}` namespace
# prefix ElementTree leaves on tag) is deliberately more permissive than
# asserting one exact namespace URI, so a sitemap using a slightly
# different (or no) namespace doesn't parse as empty.


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


@dataclass(frozen=True)
class SitemapParseResult:
    urls: list[str]  # page URLs, from a <urlset>
    sub_sitemaps: list[str]  # nested sitemap URLs, from a <sitemapindex> -- caller must fetch+parse these separately
    parse_error: str | None = None


def parse_sitemap_urls(xml_text: str) -> SitemapParseResult:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        return SitemapParseResult(urls=[], sub_sitemaps=[], parse_error=str(e))

    root_tag = _local_tag(root.tag)
    urls: list[str] = []
    sub_sitemaps: list[str] = []

    if root_tag == "sitemapindex":
        for sitemap_el in root:
            if _local_tag(sitemap_el.tag) != "sitemap":
                continue
            for loc_el in sitemap_el:
                if _local_tag(loc_el.tag) == "loc" and loc_el.text:
                    sub_sitemaps.append(loc_el.text.strip())
    else:  # urlset, or anything else -- best-effort scan for <url><loc>
        for url_el in root:
            if _local_tag(url_el.tag) != "url":
                continue
            for loc_el in url_el:
                if _local_tag(loc_el.tag) == "loc" and loc_el.text:
                    urls.append(loc_el.text.strip())

    return SitemapParseResult(urls=urls, sub_sitemaps=sub_sitemaps)
