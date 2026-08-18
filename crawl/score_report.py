"""--score-report: reads a completed (or in-progress) frontier's recorded
relevance scores and reports the distribution, per-page scores, and what
each candidate threshold would have skipped -- so real thresholds get
chosen from data after a log-only run (both thresholds at 0), not guessed
up front. Formatting is pure and separate from the DB read
(Frontier.all_scores()) so it's testable without a real frontier file.
"""
from __future__ import annotations

import statistics

CANDIDATE_THRESHOLDS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


def format_score_report(scores: list[tuple[str, float, str]]) -> str:
    """scores: (url, relevance_score, status) tuples, as returned by
    Frontier.all_scores() -- already sorted highest-score-first."""
    if not scores:
        return "No scored pages found -- nothing to report."

    values = [s for _, s, _ in scores]
    lines = [
        f"Scored pages: {len(scores)}",
        f"min={min(values):.4f}  max={max(values):.4f}  "
        f"mean={statistics.mean(values):.4f}  median={statistics.median(values):.4f}  "
        f"stdev={statistics.pstdev(values):.4f}",
        "",
        "Per-page scores (highest first):",
    ]
    for url, score, status in scores:
        lines.append(f"  {score:.4f}  [{status}]  {url}")

    lines.append("")
    lines.append("What each candidate threshold would have skipped, at log-only "
                  "(both thresholds were 0 this run, so nothing actually was):")
    for t in CANDIDATE_THRESHOLDS:
        skipped = sum(1 for s in values if s < t)
        pct = skipped / len(values) * 100
        lines.append(f"  threshold={t:.1f}:  {skipped}/{len(values)} pages would be skipped ({pct:.0f}%)")

    return "\n".join(lines)
