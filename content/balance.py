"""Source balancing (headless contract v2, Brief 3B Step 3, item 11).

No single page or site should dominate a dataset (Brief 1: release notes
were 72% of 5,986 rows; Brief 2c: the root page was 58.7% of 276). Two
mechanisms, both applied in the v2 extract path before the writer and both
recorded in result.json:

1. Low-value page detection -- changelogs / release notes and index /
   listing pages -- by a heuristic that records its reasons. Per run:
   `low_value_policy` "skip" (no extraction calls at all), "cap" (rows capped
   at `low_value_max_rows`) or "keep" (detected and reported, nothing else).
   `low_value_overrides` {"keep": [...], "skip": [...]} force a page either
   way, because a changelog is exactly the data some tasks need.

2. Row caps, per record type: `max_rows_per_page` and `max_rows_per_source`
   (a source = one site/host).

Which rows a cap keeps (deterministic for a given page's rows): ROUND-ROBIN
across the page's units -- every unit's 1st row (in chunk order), then every
unit's 2nd row, and so on, until the cap -- so coverage spreads over the
whole page instead of only its top. Kept rows are emitted in their original
order (chunk order, then the order the model returned them).

The per-source cap is applied in the order pages FINISH extraction, which
varies between runs (Step 0 decision 2, option (a)): which pages fill a
site's budget is not deterministic, so every cut is recorded (page, record
type, rows generated, rows kept).

Pure logic plus one in-memory accumulator; nothing here does I/O.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import pairwise
from urllib.parse import urlparse

import config

POLICIES = ("skip", "cap", "keep")
OVERRIDE_KINDS = ("keep", "skip")
CUTS_RECORDED_MAX = 1000   # result.json stays bounded on a huge run; the totals are always exact

_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_VERSION_HEADING = re.compile(r"^\[?v?\d+\.\d+(?:\.\d+)?\b", re.IGNORECASE)
_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")
_EXT = re.compile(r"\.(?:html?|php|aspx?|md)$", re.IGNORECASE)


@dataclass(frozen=True)
class LowValue:
    """A page's low-value verdict. kind None = not low-value."""
    kind: str | None                   # "changelog" | "index" | None
    reasons: tuple[str, ...] = ()
    action: str = "none"               # "skip" | "cap" | "keep" | "none"
    override: str | None = None        # "keep" / "skip" when low_value_overrides decided it

    def describe(self) -> str:
        what = f"low-value page ({self.kind}): {'; '.join(self.reasons)}" if self.kind else "page"
        return what + (f" [low_value_overrides: {self.override}]" if self.override else "")


def _segments(url: str) -> list[str]:
    path = urlparse(url).path
    return [_EXT.sub("", s).lower() for s in path.split("/") if s]


def detect_low_value(url: str, content: str | None, page_title: str | None = None) -> tuple[str | None, list[str]]:
    """(kind, reasons). Signals, any one is enough:
      - a URL path segment in config.LOW_VALUE_CHANGELOG_SEGMENTS /
        LOW_VALUE_INDEX_SEGMENTS (exact segment match, extension dropped),
        or a pagination path (/page/<n>);
      - the page title matching config.LOW_VALUE_CHANGELOG_TITLES /
        LOW_VALUE_INDEX_TITLES;
      - changelog shape: at least config.LOW_VALUE_MIN_VERSION_HEADINGS
        headings that start with a version number (## 0.165.0, ## v2.1,
        ## [1.2.3] - ...);
      - listing shape: at least config.LOW_VALUE_MIN_LINK_LINES non-empty
        lines carrying a markdown link, making up at least
        config.LOW_VALUE_LINK_LINE_SHARE of the non-empty lines.
    Changelog signals win over index signals when both are present."""
    found: dict[str, list[str]] = {"changelog": [], "index": []}
    segments = _segments(url)
    for seg in segments:
        if seg in config.LOW_VALUE_CHANGELOG_SEGMENTS:
            found["changelog"].append(f"url path segment '{seg}'")
        elif seg in config.LOW_VALUE_INDEX_SEGMENTS:
            found["index"].append(f"url path segment '{seg}'")
    for a, b in pairwise(segments):
        if a == "page" and b.isdigit():
            found["index"].append(f"pagination path '/page/{b}'")
    title = (page_title or "").strip().lower()
    if title:
        if any(re.search(p, title) for p in config.LOW_VALUE_CHANGELOG_TITLES):
            found["changelog"].append(f"title '{page_title.strip()[:60]}'")
        elif any(re.search(p, title) for p in config.LOW_VALUE_INDEX_TITLES):
            found["index"].append(f"title '{page_title.strip()[:60]}'")
    text = content or ""
    if text:
        headings = _HEADING.findall(text)
        versions = sum(1 for h in headings if _VERSION_HEADING.match(h.strip()))
        if versions >= config.LOW_VALUE_MIN_VERSION_HEADINGS:
            found["changelog"].append(f"{versions} of {len(headings)} headings are version numbers")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        link_lines = sum(1 for ln in lines if _LINK.search(ln))
        if (link_lines >= config.LOW_VALUE_MIN_LINK_LINES
                and link_lines / len(lines) >= config.LOW_VALUE_LINK_LINE_SHARE):
            found["index"].append(f"{link_lines} of {len(lines)} lines are links ({link_lines / len(lines):.0%})")
    if found["changelog"]:
        return "changelog", found["changelog"] + found["index"]
    if found["index"]:
        return "index", found["index"]
    return None, []


def _matches(url: str, prefix: str) -> bool:
    """A prefix is a full URL prefix ("https://site/x/") or a path prefix ("/x/")."""
    if prefix.startswith("/"):
        return urlparse(url).path.startswith(prefix)
    return url.startswith(prefix)


def override_for(url: str, overrides: dict | None) -> str | None:
    """The longest matching prefix wins; on a tie, keep."""
    best: tuple[int, str] | None = None
    for kind in OVERRIDE_KINDS:
        for prefix in (overrides or {}).get(kind, []):
            if _matches(url, prefix):
                cand = (len(prefix), kind)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and kind == "keep"):
                    best = cand
    return best[1] if best else None


def round_robin_keep(rows: list[dict], limit: int) -> list[dict]:
    """The keep rule: each unit's 1st row (chunk order), then each unit's 2nd,
    ... up to `limit`; returned in original order."""
    if limit >= len(rows):
        return list(rows)
    if limit <= 0:
        return []
    by_unit: dict[object, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        by_unit[row.get("chunk_index")].append(i)
    order = sorted(by_unit, key=lambda k: (k is None, k if isinstance(k, int) else 0))
    chosen: list[int] = []
    rnd = 0
    while len(chosen) < limit:
        for unit in order:
            if rnd < len(by_unit[unit]):
                chosen.append(by_unit[unit][rnd])
                if len(chosen) == limit:
                    break
        rnd += 1
    return [rows[i] for i in sorted(chosen)]


@dataclass
class BalanceSettings:
    max_rows_per_page: int | None = None
    max_rows_per_source: int | None = None
    low_value_policy: str = "keep"
    low_value_max_rows: int | None = None
    low_value_overrides: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, overrides: dict | None = None) -> BalanceSettings:
        """Read the applied v2 settings off `config` at call time."""
        return cls(config.MAX_ROWS_PER_PAGE, config.MAX_ROWS_PER_SOURCE, config.LOW_VALUE_POLICY,
                   config.LOW_VALUE_MAX_ROWS, dict(overrides or {}))


class Balancer:
    """One per v2 run. classify() before extraction, apply() before the
    writer. Both are synchronous (no await), so concurrent extract workers on
    one event loop can't interleave inside them."""

    def __init__(self, settings: BalanceSettings):
        self.s = settings
        self._source_rows: Counter = Counter()           # (host, record_type) -> rows passed to the writer
        self.pages: dict[str, dict] = {}                 # url -> {"generated", "kept", "low_value", ...}
        self.cuts: list[dict] = []
        self.cut_rows = 0
        self.skipped: dict[str, dict] = {}
        self.low_value: dict[str, dict] = {}              # url -> verdict, every page classify() flagged

    def seed_written(self, rows) -> None:
        """Resume: rows already in canonical.jsonl count toward the per-source cap."""
        for row in rows:
            self._source_rows[(_host(row.get("source_url", "")), row.get("record_type", "qa"))] += 1

    def classify(self, url: str, content: str | None, page_title: str | None = None) -> LowValue:
        override = override_for(url, self.s.low_value_overrides)
        kind, reasons = detect_low_value(url, content, page_title)
        if override == "skip":
            verdict = LowValue(kind, tuple(reasons), "skip", "skip")
        elif override == "keep":
            verdict = LowValue(kind, tuple(reasons), "keep" if kind else "none", "keep")
        elif kind is None:
            verdict = LowValue(None)
        else:
            verdict = LowValue(kind, tuple(reasons), self.s.low_value_policy)
        if verdict.kind or verdict.override:
            self.low_value[url] = _verdict_dict(verdict)
        if verdict.action == "skip":
            self.skipped[url] = _verdict_dict(verdict)
        return verdict

    def apply(self, url: str, rows: list[dict], verdict: LowValue | None = None) -> list[dict]:
        """Cap one page's rows (per record type), record what was cut, and
        count what is kept toward its source's cap."""
        verdict = verdict or LowValue(None)
        host = _host(url)
        by_type: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_type[row.get("record_type", "qa")].append(row)
        kept_ids: set[int] = set()
        page_entry = {"generated": len(rows), "kept": 0, "by_record_type": {}}
        for rtype, typed in by_type.items():
            limit, rule = None, None
            if self.s.max_rows_per_page is not None:
                limit, rule = self.s.max_rows_per_page, "max_rows_per_page"
            if verdict.action == "cap" and self.s.low_value_max_rows is not None and (
                    limit is None or self.s.low_value_max_rows < limit):
                limit, rule = self.s.low_value_max_rows, f"low_value_max_rows ({verdict.kind})"
            kept = typed if limit is None else round_robin_keep(typed, limit)
            if len(kept) < len(typed):
                self._cut(url, rtype, rule, len(typed), len(kept))
            if self.s.max_rows_per_source is not None:
                room = max(0, self.s.max_rows_per_source - self._source_rows[(host, rtype)])
                if len(kept) > room:
                    before = len(kept)
                    kept = round_robin_keep(kept, room)
                    self._cut(url, rtype, "max_rows_per_source", before, len(kept))
            self._source_rows[(host, rtype)] += len(kept)
            kept_ids.update(id(r) for r in kept)
            page_entry["by_record_type"][rtype] = {"generated": len(typed), "kept": len(kept)}
        out = [r for r in rows if id(r) in kept_ids]
        page_entry["kept"] = len(out)
        self.pages[url] = page_entry
        return out

    def _cut(self, url, rtype, rule, generated, kept) -> None:
        self.cut_rows += generated - kept
        if len(self.cuts) < CUTS_RECORDED_MAX:
            self.cuts.append({"url": url, "record_type": rtype, "rule": rule,
                              "rows_before": generated, "rows_kept": kept})

    def report(self) -> dict:
        """result.json's `balance` section."""
        kept = {u: p["kept"] for u, p in self.pages.items()}
        total = sum(kept.values())
        largest = min(kept.items(), key=lambda kv: (-kv[1], kv[0]), default=None)  # ties: first URL
        return {
            "settings": {"max_rows_per_page": self.s.max_rows_per_page,
                         "max_rows_per_source": self.s.max_rows_per_source,
                         "low_value_policy": self.s.low_value_policy,
                         "low_value_max_rows": self.s.low_value_max_rows,
                         "low_value_overrides": self.s.low_value_overrides},
            "keep_rule": "round-robin across a page's units (chunk order); per-source cap in page completion order",
            "rows_generated": sum(p["generated"] for p in self.pages.values()),
            "rows_kept": total,
            "rows_cut": self.cut_rows,
            "rows_per_page": dict(sorted(kept.items())),
            "largest_page": ({"url": largest[0], "rows": largest[1],
                              "share": round(largest[1] / total, 4) if total else None,
                              "pages_with_that_many_rows": sum(1 for n in kept.values() if n == largest[1])}
                             if largest else None),
            "rows_per_source": {f"{h} {t}": n for (h, t), n in sorted(self._source_rows.items())},
            "capped": self.cuts,
            "cuts_truncated": self.cut_rows > 0 and len(self.cuts) >= CUTS_RECORDED_MAX,
            "low_value_pages": dict(sorted(self.low_value.items())),
            "skipped": dict(sorted(self.skipped.items())),
        }


def rebalance_rows(rows: list[dict], settings: BalanceSettings) -> tuple[list[dict], dict]:
    """Offline recompute over an existing canonical.jsonl's rows (Step 3
    measurement): pages in the order they first appear (= the order they
    were written), page content approximated by the page's distinct
    source_chunks in chunk order (the real page text isn't stored; units
    that produced no rows are missing). A "skip" verdict drops the page."""
    pages: dict[str, list[dict]] = {}
    for row in rows:
        pages.setdefault(row["source_url"], []).append(row)
    balancer = Balancer(settings)
    kept: list[dict] = []
    for url, page_rows in pages.items():
        chunks = {r.get("chunk_index"): r.get("source_chunk", "") for r in page_rows}
        content = "\n".join(chunks[k] for k in sorted(chunks, key=lambda k: (k is None, k or 0)))
        verdict = balancer.classify(url, content, page_rows[0].get("page_title"))
        if verdict.action == "skip":
            balancer.pages[url] = {"generated": len(page_rows), "kept": 0, "by_record_type": {}}
            balancer.cut_rows += len(page_rows)
            continue
        kept.extend(balancer.apply(url, page_rows, verdict))
    return kept, balancer.report()


def _verdict_dict(v: LowValue) -> dict:
    return {"kind": v.kind, "reasons": list(v.reasons), "action": v.action, "override": v.override}


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()
