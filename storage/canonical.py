"""The canonical record: one row per Q&A pair, written once by Writer,
never a training file directly (step 8 plan, Part B, level 1). Schema
projections and framework packaging (export.py) are pure functions over
this file -- changing training format must never require re-crawling, so
every field a future projection might need has to be captured now, even
one (source_chunk) nothing currently reads.

chunk_index (position of this pair's source_chunk within the page's
extraction-unit sequence, 0-based) exists specifically so redundancy
analysis (dataset_report.py, ROADMAP #23) can classify a near-duplicate
pair as adjacent-chunk-overlap (consecutive index) vs. same-chunk
paraphrase-padding (identical source_chunk) without having to infer chunk
order from file position, which a real crash-resumed run could reorder.
"""
from __future__ import annotations

import re

CANONICAL_FIELDS = (
    "question", "answer", "source_chunk", "chunk_index", "source_url", "section",
    "page_title", "generation_model", "extraction_strategy",
    "timestamp", "crawl_date", "license_signal",
    "quality_score", "quality_decision_method",
)


def build_canonical_record(
    *,
    question: str,
    answer: str,
    source_chunk: str,
    chunk_index: int,
    source_url: str,
    section: str,
    page_title: str | None,
    generation_model: str,
    extraction_strategy: str,
    timestamp: str,
    crawl_date: str,
    license_signal: str | None,
    quality_score: float | None = None,
    quality_decision_method: str | None = None,
    scraper_version: str | None = None,
) -> dict:
    """scraper_version: headless contract v1 only (the producing scraper's git
    commit, e.g. "3437952+dirty"). None -- legacy -- leaves the record
    exactly as before (no key)."""
    record = {
        "question": question,
        "answer": answer,
        "source_chunk": source_chunk,
        "chunk_index": chunk_index,
        "source_url": source_url,
        "section": section,
        "page_title": page_title,
        "generation_model": generation_model,
        "extraction_strategy": extraction_strategy,
        "timestamp": timestamp,
        "crawl_date": crawl_date,
        "license_signal": license_signal,
        # Step 4 traceability: which decision method (traditional/jev/laya)
        # judged this pair's quality, and its raw noul value -- None/None
        # when sample quality scoring wasn't enabled for this run (the
        # common case today; see content/sample_quality.py -- opt-in, not
        # a default-on behavior change to every existing crawl).
        "quality_score": quality_score,
        "quality_decision_method": quality_decision_method,
    }
    if scraper_version is not None:
        record["scraper_version"] = scraper_version
    return record


# --- canonical row schema v2 (headless contract v2, Brief 3B) ---------------
# v1 rows (above) carry no schema_version and stay byte-identical. v2 rows add
# a header (schema_version, record_type), keep the generator's own fields and
# the common fields at the top level (so exporters still find question/answer
# on qa rows), and move scraper_version into a provenance block that says
# exactly what produced the row and from where.
SCHEMA_VERSION_V2 = 2
PROVENANCE_FIELDS = ("generator", "provider", "model", "pinned_version", "served_model", "endpoint",
                     "scraper_version", "run_id", "prompt_sha256")


def build_canonical_record_v2(fields: dict, *, record_type: str, provenance: dict,
                              served_model: str | None) -> dict:
    """fields: what build_canonical_record takes (scraper_version moves into
    provenance). provenance: every PROVENANCE_FIELDS key except served_model,
    which is per call (None when the provider's response named no model)."""
    missing = [k for k in PROVENANCE_FIELDS if k != "served_model" and k not in provenance]
    if missing:
        raise ValueError(f"provenance missing {missing}")
    record = {"schema_version": SCHEMA_VERSION_V2, "record_type": record_type}
    record.update({k: v for k, v in fields.items() if k != "scraper_version"})
    record["provenance"] = {k: (served_model if k == "served_model" else provenance[k]) for k in PROVENANCE_FIELDS}
    return record


_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[.*?\]\(.*?\)")


def extract_page_title(content: str) -> str | None:
    """First markdown H1 in the stripped content, with the trailing
    anchor-link markup chrome_strip/crawl4ai leave on headings
    (e.g. "[|](...)") stripped back out -- otherwise every title would
    carry a dangling link fragment."""
    match = _H1_RE.search(content)
    if not match:
        return None
    title = _MD_LINK_RE.sub("", match.group(1)).strip()
    return title or None


# Best-effort text match, not a legal determination -- surfaces whatever
# license/terms phrase a page's own footer/meta text exposes, if any, so a
# downstream consumer has a signal to check manually rather than nothing.
_LICENSE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"creative commons(?:\s+attribution)?(?:[\s-]+\S+){0,4}",
        r"CC[\s-]BY(?:-[A-Z]+)*(?:\s+\d(?:\.\d)?)?",
        r"MIT license",
        r"apache license(?:,?\s+version\s+[\d.]+)?",
        r"all rights reserved",
        r"©\s*\d{4}[^\n]{0,80}",
    ]
]


def detect_license_signal(content: str) -> str | None:
    for pattern in _LICENSE_PATTERNS:
        match = pattern.search(content)
        if match:
            return match.group(0).strip()
    return None
