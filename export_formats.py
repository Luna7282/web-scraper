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
from typing import Callable

from chunk_store import normalize_chunk_text

DEFAULT_MIN_ANSWER_LENGTH = 20


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
