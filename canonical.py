"""The canonical record: one row per Q&A pair, written once by Writer,
never a training file directly (step 8 plan, Part B, level 1). Schema
projections and framework packaging (export.py) are pure functions over
this file -- changing training format must never require re-crawling, so
every field a future projection might need has to be captured now, even
one (source_chunk) nothing currently reads.
"""
from __future__ import annotations

import re

CANONICAL_FIELDS = (
    "question", "answer", "source_chunk", "source_url", "section",
    "page_title", "generation_model", "extraction_strategy",
    "timestamp", "crawl_date", "license_signal",
)


def build_canonical_record(
    *,
    question: str,
    answer: str,
    source_chunk: str,
    source_url: str,
    section: str,
    page_title: str | None,
    generation_model: str,
    extraction_strategy: str,
    timestamp: str,
    crawl_date: str,
    license_signal: str | None,
) -> dict:
    return {
        "question": question,
        "answer": answer,
        "source_chunk": source_chunk,
        "source_url": source_url,
        "section": section,
        "page_title": page_title,
        "generation_model": generation_model,
        "extraction_strategy": extraction_strategy,
        "timestamp": timestamp,
        "crawl_date": crawl_date,
        "license_signal": license_signal,
    }


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
