"""--dataset-report: reads a completed run's canonical.jsonl (and,
optionally, the frontier DB it came from) and reports pair-level
statistics -- the instrument for ROADMAP #23's redundancy question.
Offline, no LLM calls: pure reads over what a run already wrote to disk,
so it's safe to run against any completed crawl and fully testable
against synthetic records.
"""
from __future__ import annotations

import statistics
from collections import Counter
from difflib import SequenceMatcher
from re import sub as _sub

from export.export_formats import dedup_by_question

# Picked from step 8 Part A's live-check analysis (LESSONS_LEARNED.md
# #26), not a calibrated/labeled threshold -- questions are short enough
# that 0.6 caught real duplicates without much noise; answers are longer,
# so a real duplicate shares a lower fraction of its total characters.
QUESTION_NEAR_DUP_THRESHOLD = 0.6
ANSWER_NEAR_DUP_THRESHOLD = 0.4


def _normalize(text: str) -> str:
    return _sub(r"[^a-z0-9 ]", "", (text or "").lower())


def pairs_per_page(records: list[dict]) -> dict[str, int]:
    return dict(Counter(r.get("source_url", "") for r in records))


def find_near_duplicates(
    records: list[dict], field: str, threshold: float,
) -> list[tuple[int, int, float]]:
    """(i, j, ratio) for every pair of records whose normalized `field`
    text is at least `threshold` similar -- restricted to records sharing
    the same source_url. Comparing across unrelated pages produces
    matches on generic question templates ("Who are the authors of...")
    that inflate the count with false positives; that's exactly what step
    8 Part A's live-check had to work around by hand when it first tried
    a blanket all-pairs comparison.

    ponytail: O(n^2) pairwise SequenceMatcher per page, fine for a capped
    crawl's few hundred rows total -- but a single long reference page
    under per_chunk can itself produce 100-150+ pairs (confirmed on
    docs.manim.community's real Part D run), so real per-page n^2 needed
    a cheaper pre-filter, not just "small enough to ignore." quick_ratio()
    is a fast upper bound on ratio() (never lower) -- skipping straight to
    the full O(n*m) ratio() only when quick_ratio() already clears the
    threshold cuts most pairs (which aren't close) at a fraction of the
    cost, without changing which pairs end up counted as near-duplicates.
    A much larger corpus would still need an indexed approach (minhash /
    embedding clustering) instead of this.
    """
    by_url: dict[str, list[int]] = {}
    for i, r in enumerate(records):
        by_url.setdefault(r.get("source_url", ""), []).append(i)

    matchers = [SequenceMatcher(None, "", _normalize(r.get(field, ""))) for r in records]
    dups = []
    for indices in by_url.values():
        for a in range(len(indices)):
            for b in range(a + 1, len(indices)):
                i, j = indices[a], indices[b]
                matchers[i].set_seq1(matchers[j].b)
                if matchers[i].quick_ratio() < threshold:
                    continue
                ratio = matchers[i].ratio()
                if ratio >= threshold:
                    dups.append((i, j, ratio))
    return dups


def classify_redundancy_cause(records: list[dict], i: int, j: int) -> str:
    """same_chunk: both pairs came from the literal same source_chunk
    text (the "3 to 5 diverse pairs" padding case -- not a chunk-overlap
    artifact at all). adjacent_chunk: same page, consecutive chunk_index
    (the ~10%-overlap boundary case). other: same page but non-adjacent,
    or (shouldn't happen given find_near_duplicates already restricts to
    one url, handled defensively anyway) different pages entirely."""
    a, b = records[i], records[j]
    if a.get("source_url") != b.get("source_url"):
        return "other"
    if a.get("source_chunk") == b.get("source_chunk"):
        return "same_chunk"
    ai, bi = a.get("chunk_index"), b.get("chunk_index")
    if ai is not None and bi is not None and abs(ai - bi) == 1:
        return "adjacent_chunk"
    return "other"


def redundancy_by_cause(
    records: list[dict], field: str = "answer", threshold: float = ANSWER_NEAR_DUP_THRESHOLD,
) -> dict[str, int]:
    dups = find_near_duplicates(records, field, threshold)
    return dict(Counter(classify_redundancy_cause(records, i, j) for i, j, _ in dups))


def build_dataset_report(records: list[dict], scores: list[tuple[str, float, str]] | None = None) -> str:
    lines = [f"Total canonical pairs: {len(records)}"]

    per_page = pairs_per_page(records)
    if per_page:
        counts = list(per_page.values())
        lines.append(
            f"Pages with pairs: {len(per_page)}  pairs/page min={min(counts)} "
            f"max={max(counts)} mean={statistics.mean(counts):.1f}"
        )
    lines.append("")

    _, exact_dup_removed = dedup_by_question(records)
    lines.append(f"Exact-duplicate questions (normalized text): {exact_dup_removed}")

    q_dups = find_near_duplicates(records, "question", QUESTION_NEAR_DUP_THRESHOLD)
    a_dups = find_near_duplicates(records, "answer", ANSWER_NEAR_DUP_THRESHOLD)
    lines.append(f"Near-duplicate questions (ratio>={QUESTION_NEAR_DUP_THRESHOLD}, same page): {len(q_dups)}")
    lines.append(f"Near-duplicate answers (ratio>={ANSWER_NEAR_DUP_THRESHOLD}, same page): {len(a_dups)}")
    lines.append("")

    causes = redundancy_by_cause(records)
    lines.append("Redundancy by cause (near-duplicate-answer pairs, classified):")
    for cause in ("same_chunk", "adjacent_chunk", "other"):
        lines.append(f"  {cause}: {causes.get(cause, 0)}")
    lines.append("")

    if scores:
        lines.append("Relevance scores vs. what was extracted (score, status, pairs, url):")
        for url, score, status in scores:
            lines.append(f"  {score:.4f}  [{status}]  pairs={per_page.get(url, 0)}  {url}")

    return "\n".join(lines)
