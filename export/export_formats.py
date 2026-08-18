"""Pure functions over canonical records (canonical.py) -- schema
projections and the cross-cutting concerns that have to run before any
projection is written out (step 8 plan, Part B, levels 1-2).

Nothing here touches a file or the network; export.py's CLI is what wires
these into actual output. Keeping them pure is what makes "changing
training format must never require re-crawling" literally true: every
function here takes canonical records already on disk and returns new
in-memory structures, nothing more.
"""
from __future__ import annotations

import random
from difflib import SequenceMatcher
from re import sub as _sub
from typing import Callable

from storage.chunk_store import normalize_chunk_text

DEFAULT_MIN_ANSWER_LENGTH = 20

# Picked from step 8 Part A's live-check analysis (LESSONS_LEARNED.md
# #26), not a calibrated/labeled threshold -- answers are long enough
# that a real duplicate shares a lower fraction of its total characters
# than a real duplicate question does. Also semantic_dedup()'s default
# drop threshold (Phase 3 Step 4) -- the same measure serves both a
# report (dataset_report.py) and an actual removal decision, so a
# dry_run mode exists precisely so this default isn't trusted blind for
# the latter. QUESTION_NEAR_DUP_THRESHOLD (0.6) stays in
# dataset_report.py -- it's report-only, nothing removes rows by it.
ANSWER_NEAR_DUP_THRESHOLD = 0.4


def _normalize_near_dup_text(text: str) -> str:
    return _sub(r"[^a-z0-9 ]", "", (text or "").lower())


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

    matchers = [SequenceMatcher(None, "", _normalize_near_dup_text(r.get(field, ""))) for r in records]
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


def validate_records(
    records: list[dict], min_answer_length: int = DEFAULT_MIN_ANSWER_LENGTH,
) -> tuple[list[dict], list[dict]]:
    """Rejects empty answers, question==answer, and below-minimum-length
    answers. Returns (valid, rejected) -- rejected keeps every field plus
    a _reject_reason, so a dataset card can report why rows were dropped
    instead of the count just silently shrinking."""
    valid, rejected = [], []
    for r in records:
        question = (r.get("question") or "").strip()
        answer = (r.get("answer") or "").strip()
        if not answer:
            reason = "empty_answer"
        elif question.lower() == answer.lower():
            reason = "question_equals_answer"
        elif len(answer) < min_answer_length:
            reason = "answer_too_short"
        else:
            valid.append(r)
            continue
        rejected.append({**r, "_reject_reason": reason})
    return valid, rejected


def dedup_by_question(records: list[dict]) -> tuple[list[dict], int]:
    """Global dedup on normalized question text at export time. Reuses
    chunk_store.py's normalization (whitespace/case/trailing-punctuation)
    rather than defining a second, possibly-drifting definition of
    "the same question" -- the same collision rule already proven against
    real duplicate content, applied to questions instead of chunks."""
    seen: set[str] = set()
    deduped = []
    for r in records:
        key = normalize_chunk_text(r.get("question", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped, len(records) - len(deduped)


def semantic_dedup(
    records: list[dict],
    *,
    field: str = "answer",
    threshold: float = ANSWER_NEAR_DUP_THRESHOLD,
    dry_run: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Pair-level near-duplicate removal, run after dedup_by_question
    (exact question match, above) since it catches what that stage
    demonstrably misses: rewordings of the same underlying fact, not just
    identical questions. Covers both LESSONS_LEARNED.md #33's causes --
    same-chunk paraphrase padding and adjacent-chunk overlap -- in one
    mechanism, since both show up as answer-text near-duplicates on the
    same page regardless of which chunk(s) produced them; this is why
    split chunking was rejected in favor of this instead.

    Reuses find_near_duplicates() -- the exact detector already
    calibrated against real data for the dataset_report.py redundancy
    measurement, restricted to same-source_url pairs and pre-filtered by
    quick_ratio() for the O(n^2) cost.

    Collision rule: the longer answer survives, not whichever was
    written first -- the 2B manual read (LESSONS_LEARNED.md #33) found a
    near-dup group where a later pair consolidated several rewordings
    into the single most complete answer, so "first" is not a reliable
    proxy for "best" here. Ties keep the lower record index for
    determinism. A record that loses one comparison but wins another
    (a redundant cluster of 3+) is still dropped -- only the single
    longest answer in a mutually-similar group survives.

    dry_run=True runs identical detection and collision logic but
    returns every record unchanged in the first slot and the drop
    decisions in the second, so a threshold can be picked from a real
    report instead of guessed -- see export.py's --semantic-dedup-report."""
    dups = find_near_duplicates(records, field, threshold)
    if not dups:
        return records, []

    drop_reasons: dict[int, dict] = {}
    for i, j, ratio in dups:
        len_i = len((records[i].get(field) or ""))
        len_j = len((records[j].get(field) or ""))
        winner, loser = (i, j) if len_i >= len_j else (j, i)
        existing = drop_reasons.get(loser)
        if existing is None or ratio > existing["ratio"]:
            drop_reasons[loser] = {"winner": winner, "ratio": ratio}

    dropped = [
        {
            **records[loser],
            "_dedup_reason": "semantic_near_duplicate",
            "_dedup_ratio": round(info["ratio"], 3),
            "_dedup_survivor_question": records[info["winner"]].get("question"),
        }
        for loser, info in drop_reasons.items()
    ]
    if dry_run:
        return records, dropped

    deduped = [r for idx, r in enumerate(records) if idx not in drop_reasons]
    return deduped, dropped


def split_records(
    records: list[dict],
    *,
    by: str = "section",
    ratios: tuple[float, float, float] = (0.8, 0.1, 0.1),
    seed: int = 42,
) -> dict[str, list[dict]]:
    """Splits by grouping on `by` (section or source_url) so every row from
    one group lands entirely in one split. Docs repeat content across
    pages/sections -- a random per-row split would leak near-duplicates
    into eval and inflate the reported numbers. Seeded so a re-export of
    the same canonical file reproduces the same split.

    Small datasets (few distinct groups) can produce an empty validation
    or test split after rounding -- an accepted limitation, not silently
    hidden: the caller sees empty lists, not a crash or a stratified
    rebalance this function doesn't attempt.
    """
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(r.get(by) or "unknown", []).append(r)

    keys = sorted(groups.keys())
    random.Random(seed).shuffle(keys)

    n = len(keys)
    train_end = round(n * ratios[0])
    val_end = train_end + round(n * ratios[1])
    train_keys, val_keys, test_keys = keys[:train_end], keys[train_end:val_end], keys[val_end:]

    def flatten(ks: list[str]) -> list[dict]:
        out = []
        for k in ks:
            out.extend(groups[k])
        return out

    return {"train": flatten(train_keys), "validation": flatten(val_keys), "test": flatten(test_keys)}


# ---------------------------------------------------------------------------
# Schema projections -- pure functions, one canonical record in, one
# projected record out (raw_text and triplets are the exceptions: they
# need more than a single record, noted on each).
# ---------------------------------------------------------------------------

def to_conversational(r: dict) -> dict:
    return {"messages": [
        {"role": "user", "content": r["question"]},
        {"role": "assistant", "content": r["answer"]},
    ]}


def to_alpaca(r: dict) -> dict:
    return {"instruction": r["question"], "input": "", "output": r["answer"]}


def to_prompt_completion(r: dict) -> dict:
    return {"prompt": r["question"], "completion": r["answer"]}


def to_raw_text(records: list[dict]) -> list[dict]:
    """From deduplicated source chunks, not question/answer pairs -- for
    continued pretraining, not instruction tuning. Dedups on the chunk
    text itself (a chunk shared by multiple pairs, or repeated across
    per_chunk units from overlapping windows, should appear once)."""
    seen: set[str] = set()
    out = []
    for r in records:
        chunk = r.get("source_chunk", "")
        key = normalize_chunk_text(chunk)
        if key in seen or not chunk:
            continue
        seen.add(key)
        out.append({"text": chunk})
    return out


def to_embedding_pair(r: dict) -> dict:
    return {"anchor": r["question"], "positive": r["source_chunk"]}


def to_triplet(r: dict, negative_chunk: str) -> dict:
    return {"anchor": r["question"], "positive": r["source_chunk"], "negative": negative_chunk}


def mine_triplets(records: list[dict], seed: int = 42) -> list[dict]:
    """One triplet per record; the negative is a source_chunk mined from a
    DIFFERENT source_url, chosen at random -- restricting to a different
    page (not just a different chunk) guards against picking a
    same-page neighbor that might still be topically relevant, which
    would make it a weak negative."""
    rng = random.Random(seed)
    by_url: dict[str, list[str]] = {}
    for r in records:
        by_url.setdefault(r["source_url"], []).append(r["source_chunk"])

    triplets = []
    for r in records:
        other_chunks = [c for url, chunks in by_url.items() if url != r["source_url"] for c in chunks]
        if not other_chunks:
            continue
        triplets.append(to_triplet(r, rng.choice(other_chunks)))
    return triplets


def to_rag_eval(r: dict) -> dict:
    return {"question": r["question"], "ground_truth_answer": r["answer"], "context": r["source_chunk"]}


def to_openai_finetune(r: dict) -> dict:
    # Same {"messages": [...]} shape OpenAI's chat fine-tuning JSONL expects.
    return to_conversational(r)


def to_vertex(r: dict) -> dict:
    return {"contents": [
        {"role": "user", "parts": [{"text": r["question"]}]},
        {"role": "model", "parts": [{"text": r["answer"]}]},
    ]}


SCHEMA_PROJECTIONS: dict[str, Callable[[dict], dict]] = {
    "conversational": to_conversational,
    "alpaca": to_alpaca,
    "prompt_completion": to_prompt_completion,
    "embedding_pairs": to_embedding_pair,
    "rag_eval": to_rag_eval,
    "openai_finetune": to_openai_finetune,
    "vertex": to_vertex,
}

# raw_text and triplets take the whole record list, not one record at a
# time -- raw_text dedups across records, triplets mines a negative from
# a different page. Kept out of SCHEMA_PROJECTIONS (one-record-in shape)
# rather than forced into it.
BATCH_PROJECTIONS = {
    "raw_text": to_raw_text,
    "triplets": mine_triplets,
}

# Declared explicitly as unsupported rather than attempted badly: each of
# these needs information the canonical record doesn't carry, from a pass
# this export layer doesn't run.
UNSUPPORTED_WITHOUT_EXTRA_PASS = {
    "dpo": "needs a rejected answer -- a second generation pass, not a projection of the canonical record alone",
    "orpo": "same requirement as DPO -- a rejected answer from a second generation pass",
    "kto": "needs a binary desirability label per example, not present in the canonical schema",
    "classification": "needs class labels, not present in the canonical schema",
}
