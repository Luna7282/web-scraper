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
  - licence: EVIDENCE only (a snippet and the page it came from): rel=license
    links, Creative Commons licence links, and common licence names in the
    raw HTML's head and tail (footers are stripped from the markdown, so the
    raw HTML is where licences actually are). Never a legal determination.
  - checked_at: when robots.txt was fetched (UTC).

Everything here is pure or in-memory; main.py feeds it from the v2 fetch path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

import config
from crawl.robots_cache import _is_path_allowed, _parse_rules

NOTHING_FOUND = "none_found"
ROBOTS_RESULTS = ("allowed", "partially_disallowed", "disallowed", "no_robots_txt", "fetch_failed")
SNIPPET_MAX = 160
EVIDENCE_PER_KIND = 3
HTML_HEAD_SCAN = 50_000
HTML_TAIL_SCAN = 100_000

_CONTENT_SIGNAL = re.compile(r"^\s*content-signal\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_USER_AGENT = re.compile(r"^\s*user-agent\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_META_ROBOTS = re.compile(r"<meta\b[^>]*name\s*=\s*[\"'](?:robots|[a-z-]*bot)[\"'][^>]*>", re.IGNORECASE)
_NOAI = re.compile(r"\bno(?:image)?ai\b", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_LICENCE_PATTERNS = (
    ("rel=license link", re.compile(r"<(?:a|link)\b[^>]*\brel\s*=\s*[\"'][^\"']*\blicense\b[^\"']*[\"'][^>]*>", re.IGNORECASE)),
    ("Creative Commons licence link",
     re.compile(r"creativecommons\.org/licenses/[a-z-]+/\d(?:\.\d)?", re.IGNORECASE)),
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


def licence_evidence(html: str | None, page_url: str) -> list[dict]:
    """Licence evidence in the raw HTML's head and tail: kind, snippet, page."""
    html = html or ""
    region = html if len(html) <= HTML_HEAD_SCAN + HTML_TAIL_SCAN else (
        html[:HTML_HEAD_SCAN] + "\n" + html[-HTML_TAIL_SCAN:])
    out = []
    for kind, pattern in _LICENCE_PATTERNS:
        m = pattern.search(region)
        if m is None:
            continue
        lo, hi = max(0, m.start() - 60), min(len(region), m.end() + 60)
        snippet = " ".join(_TAGS.sub(" ", region[lo:hi]).split()) if "<" not in m.group(0)[:1] else m.group(0)
        out.append({"kind": kind, "snippet": snippet[:SNIPPET_MAX], "url": page_url})
    return out


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
        site = self._site(urlparse(url).netloc)
        for tag in page_noai_tags(headers, html):
            if len(site.noai_pages) < EVIDENCE_PER_KIND * 3:
                site.noai_pages.append({"url": url, "tag": tag})
        for ev in licence_evidence(html, url):
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
