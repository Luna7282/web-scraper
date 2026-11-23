"""Per-site source records (headless contract v2, Brief 3B Step 1, item 16).

One record per site the run contacted, in the shape foundry's
`runs.data_sources_json` requires: site, robots_txt, ai_training_signal,
licence, checked_at -- never a missing field; "we looked and found nothing"
is the explicit value NOTHING_FOUND. Supporting evidence goes in `details`.

What is read, and from where:
  - robots_txt: the robots.txt HTTP status (200 / 404 / other) plus which
    crawled URLs it disallowed (frontier rows failed with
    ROBOTS_DISALLOWED_ERROR), as one of ROBOTS_RESULTS.
  - ai_training_signal: robots.txt `Content-Signal:` lines (ROADMAP #38),
    robots.txt groups that name AI crawlers explicitly
    (config.AI_TRAINING_USER_AGENTS), and `noai` / `noimageai` in a page's
    X-Robots-Tag header or robots meta tag. Reported as found, never
    interpreted or enforced.
  - licence: EVIDENCE only (a snippet and the page it came from), and only
    SITE-LEVEL evidence in the raw HTML (footers are stripped from the
    markdown, so the raw HTML is where licences actually are): a
    rel="license" link, a link to a licence page or file (LICENSE,
    /licence/, creativecommons.org/licenses/..., opensource.org/licenses/...),
    or a licence name in the page footer (<footer>, role="contentinfo", or an
    element whose id/class contains "footer"). Ignored: HTML comments and
    anything inside <svg>, <script>, <style> or <template> -- e.g. Font
    Awesome's licence comment in an inline icon is the icon set's licence,
    not the site's (ROADMAP #63). Licence names in body text don't count
    either. Never a legal determination.
  - checked_at: when robots.txt was fetched (UTC).

Everything here is pure or in-memory; main.py feeds it from the v2 fetch path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

import config
from crawl.robots_cache import _is_path_allowed, _parse_rules

NOTHING_FOUND = "none_found"
ROBOTS_RESULTS = ("allowed", "partially_disallowed", "disallowed", "no_robots_txt", "fetch_failed")
SNIPPET_MAX = 160
EVIDENCE_PER_KIND = 3
HTML_HEAD_SCAN = 50_000

_CONTENT_SIGNAL = re.compile(r"^\s*content-signal\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_USER_AGENT = re.compile(r"^\s*user-agent\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_META_ROBOTS = re.compile(r"<meta\b[^>]*name\s*=\s*[\"'](?:robots|[a-z-]*bot)[\"'][^>]*>", re.IGNORECASE)
_NOAI = re.compile(r"\bno(?:image)?ai\b", re.IGNORECASE)
_CC_LINK = re.compile(r"creativecommons\.org/licenses/[a-z-]+/\d(?:\.\d)?", re.IGNORECASE)
_OSI_LINK = re.compile(r"opensource\.org/licenses?/", re.IGNORECASE)
_LICENCE_FILE = re.compile(r"^(?:licen[cs]e|copying)(?:[._-][a-z0-9]+)*(?:\.(?:md|txt|rst|html?))?$", re.IGNORECASE)
_SKIP_TAGS = frozenset({"svg", "script", "style", "template", "noscript"})
_FOOTER_TOKEN = re.compile(r"(?:^|[-_])footer(?:$|[-_])")
_NOT_FOOTER_CONTAINERS = frozenset({"html", "body", "main"})   # a body class "has-footer" isn't the footer
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                        "source", "track", "wbr"})
# Licence names, looked for in footer text only.
_LICENCE_NAMES = (
    ("MIT License", re.compile(r"\bMIT License\b|\blicensed under (?:the )?MIT\b", re.IGNORECASE)),
    ("Apache License 2.0", re.compile(r"\bApache License,?\s+Version 2\.0\b|\bApache-2\.0\b", re.IGNORECASE)),
    ("GNU GPL family", re.compile(r"\bGNU (?:Affero |Lesser )?General Public License\b", re.IGNORECASE)),
    ("BSD licence", re.compile(r"\bBSD[- ][23]-Clause\b", re.IGNORECASE)),
    ("Creative Commons named", re.compile(r"\bCC[- ]BY(?:-(?:SA|NC|ND))*(?:[- ]\d\.\d)?\b|\bCreative Commons Attribution\b")),
)


def utc_iso(ts: float | None = None) -> str:
    dt = datetime.now(UTC) if ts is None else datetime.fromtimestamp(ts, UTC)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_content_signals(robots_text: str) -> list[str]:
    """Raw values of every `Content-Signal:` line (e.g. 'ai-train=yes, search=yes')."""
    return [m.group(1) for m in _CONTENT_SIGNAL.finditer(robots_text or "")]


def ai_agent_rules(robots_text: str, agents: tuple[str, ...] | None = None) -> list[str]:
    """For each AI crawler a robots.txt group names explicitly: whether the
    site root is allowed for it. Agents only covered by `*` are not listed
    (that's ordinary robots.txt, not an AI-specific statement)."""
    agents = config.AI_TRAINING_USER_AGENTS if agents is None else agents
    named = {m.group(1).split("#", 1)[0].strip().lower() for m in _USER_AGENT.finditer(robots_text or "")}
    out = []
    for agent in agents:
        if agent.lower() not in named:
            continue
        rules, _delay = _parse_rules(robots_text, agent)
        out.append(f"{agent}: root {'allowed' if _is_path_allowed(rules, '/') else 'disallowed'}")
    return out


def page_noai_tags(headers, html: str | None) -> list[str]:
    """`noai` / `noimageai` in the X-Robots-Tag header or a robots meta tag."""
    found = []
    for key, value in (headers or {}).items() if hasattr(headers, "items") else []:
        if str(key).lower() == "x-robots-tag" and _NOAI.search(str(value)):
            found.append(f"x-robots-tag: {str(value)[:80]}")
    head = (html or "")[:HTML_HEAD_SCAN]
    for tag in _META_ROBOTS.findall(head):
        if _NOAI.search(tag):
            found.append(f"meta: {tag[:120]}")
    return found


def _is_licence_href(href: str) -> str | None:
    """The kind of licence link an href is, or None."""
    if _CC_LINK.search(href):
        return "Creative Commons licence link"
    if _OSI_LINK.search(href):
        return "licence link"
    path = urlparse(href).path.rstrip("/")
    last = path.rsplit("/", 1)[-1] if path else ""
    return "licence link" if last and _LICENCE_FILE.match(last) else None


class _LicenceScan(HTMLParser):
    """One pass over the raw HTML. Comments never reach handle_data (they go
    to handle_comment, which is not overridden), so they are ignored."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []   # (tag, opens a skip zone, opens a footer)
        self.skip = 0
        self.footer = 0
        self.footer_text: list[str] = []
        self.links: list[tuple[str, str, str]] = []    # (kind, href, rel)
        self._anchor: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        is_skip = tag in _SKIP_TAGS
        tokens = f"{a.get('id', '')} {a.get('class', '')}".lower().split()
        is_footer = not is_skip and (tag == "footer" or a.get("role", "").lower() == "contentinfo" or (
            tag not in _NOT_FOOTER_CONTAINERS and any(_FOOTER_TOKEN.search(t) for t in tokens)))
        if not self.skip and not is_skip and tag in ("a", "link"):
            href, rel = a.get("href", "").strip(), a.get("rel", "").lower().split()
            if href or "license" in rel:
                if "license" in rel:
                    self.links.append(("rel=license link", href, "license"))
                elif tag == "a" and (kind := _is_licence_href(href)):
                    self.links.append((kind, href, ""))
        if tag in _VOID_TAGS:
            return
        self.stack.append((tag, is_skip, is_footer))
        self.skip += is_skip
        self.footer += is_footer

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS and self.stack and self.stack[-1][0] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not any(t == tag for t, _, _ in self.stack):
            return  # stray end tag
        while self.stack:
            t, was_skip, was_footer = self.stack.pop()
            self.skip -= was_skip
            self.footer -= was_footer
            if t == tag:
                break

    def handle_data(self, data):
        if self.footer and not self.skip and data.strip():
            self.footer_text.append(data)


def page_licence_scan(html: str | None) -> tuple[list[tuple[str, str, str]], str]:
    """(licence links, footer text) from raw HTML. Pure; parse errors give what was read so far."""
    scan = _LicenceScan()
    try:
        scan.feed(html or "")
        scan.close()
    except Exception:  # noqa: BLE001, S110 -- malformed HTML: keep what was parsed
        pass
    return scan.links, " ".join(" ".join(scan.footer_text).split())


def licence_evidence(html: str | None, page_url: str) -> list[dict]:
    """Site-level licence evidence: kind, where, snippet, page. One item per
    kind (the first found)."""
    links, footer = page_licence_scan(html)
    out, seen = [], set()
    for kind, href, rel in links:
        if kind in seen:
            continue
        seen.add(kind)
        snippet = f'rel="license" href="{href}"' if rel else f'href="{href}"'
        out.append({"kind": kind, "where": "rel=license" if rel else "licence link",
                    "snippet": snippet[:SNIPPET_MAX], "url": page_url})
    for kind, pattern in _LICENCE_NAMES:
        m = pattern.search(footer)
        if m is None or kind in seen:
            continue
        seen.add(kind)
        lo, hi = max(0, m.start() - 60), min(len(footer), m.end() + 60)
        out.append({"kind": kind, "where": "footer", "snippet": footer[lo:hi][:SNIPPET_MAX], "url": page_url})
    return out


def page_evidence(url: str, headers, html: str | None) -> dict:
    """Everything a page contributes to its site's source record. Pure and
    CPU-bound (a full HTML parse): call it via asyncio.to_thread."""
    return {"noai_tags": page_noai_tags(headers, html), "licence": licence_evidence(html, url)}


@dataclass
class _Site:
    robots_url: str | None = None
    robots_status: int | None = None       # None = request failed
    robots_text: str = ""
    checked_at: str | None = None
    noai_pages: list[dict] = field(default_factory=list)
    licence: dict = field(default_factory=dict)   # kind -> [evidence, ...]


class SourceRecorder:
    """Collects what the v2 fetch path saw, per site, then builds the records."""

    def __init__(self, clock=None):
        self._sites: dict[str, _Site] = {}
        self._clock = clock

    def _site(self, host: str) -> _Site:
        return self._sites.setdefault(host.lower(), _Site())

    def record_robots(self, url: str, status: int | None, text: str | None) -> None:
        site = self._site(urlparse(url).netloc)
        site.robots_url, site.robots_status, site.robots_text = url, status, text or ""
        site.checked_at = utc_iso(self._clock() if self._clock else None)

    def observe_page(self, url: str, headers, html: str | None) -> None:
        self.record_evidence(url, page_evidence(url, headers, html))

    def record_evidence(self, url: str, evidence: dict) -> None:
        """Store page_evidence()'s result (cheap; runs on the event loop --
        the HTML parsing in page_evidence runs in a thread, see main.py)."""
        site = self._site(urlparse(url).netloc)
        for tag in evidence["noai_tags"]:
            if len(site.noai_pages) < EVIDENCE_PER_KIND * 3:
                site.noai_pages.append({"url": url, "tag": tag})
        for ev in evidence["licence"]:
            bucket = site.licence.setdefault(ev["kind"], [])
            if len(bucket) < EVIDENCE_PER_KIND:
                bucket.append(ev)

    def build(self, disallowed: dict[str, dict]) -> list[dict]:
        """disallowed: host -> {"urls": n disallowed rows, "seeds": n seeds,
        "seeds_disallowed": n} (Frontier.robots_disallowed_by_host)."""
        records = []
        for host in sorted(self._sites):
            site = self._sites[host]
            if site.robots_url is None:
                continue  # never contacted (robots.txt is always fetched before a page)
            counts = disallowed.get(host, {"urls": 0, "seeds": 0, "seeds_disallowed": 0})
            signals = parse_content_signals(site.robots_text) if site.robots_status == 200 else []
            agents = ai_agent_rules(site.robots_text) if site.robots_status == 200 else []
            parts = []
            if signals:
                parts.append("content-signal: " + "; ".join(signals))
            if agents:
                parts.append("robots: " + ", ".join(agents))
            if site.noai_pages:
                parts.append(f"noai tag on {len({p['url'] for p in site.noai_pages})} page(s)")
            kinds = sorted(site.licence)
            records.append({
                "site": host,
                "robots_txt": _robots_result(site.robots_status, counts),
                "ai_training_signal": " | ".join(parts) if parts else NOTHING_FOUND,
                "licence": (f"evidence only (not a legal determination): {', '.join(kinds)}" if kinds
                            else NOTHING_FOUND),
                "checked_at": site.checked_at,
                "details": {
                    "robots_url": site.robots_url, "robots_status": site.robots_status,
                    "content_signal_raw": signals, "ai_agent_rules": agents, "noai_tags": site.noai_pages,
                    "disallowed_urls": counts["urls"],
                    "licence_evidence": [ev for k in kinds for ev in site.licence[k]],
                },
            })
        return records


def _robots_result(status: int | None, counts: dict) -> str:
    if status == 404:
        return "no_robots_txt"
    if status != 200:
        return "fetch_failed"
    if counts["seeds"] and counts["seeds_disallowed"] == counts["seeds"]:
        return "disallowed"
    if counts["urls"]:
        return "partially_disallowed"
    return "allowed"
