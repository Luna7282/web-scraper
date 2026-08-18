"""Relevance scoring: how well a page matches the user's intent.

Pure math (cosine_similarity, extract_headings, chunk_text) is kept
separate from the embedding I/O (injected via EmbedFn) so the scoring
mechanics are unit-testable with a stub, independent of which strategy
(whole-page / headings / max-over-chunks) turns out to actually
discriminate -- that choice is decided by measurement (see
tests/measure_relevance_distribution.py), not assumed here.
"""
from __future__ import annotations

import math
from typing import Awaitable, Callable

from langchain_text_splitters import RecursiveCharacterTextSplitter

EmbedFn = Callable[[str], Awaitable[list[float]]]

# nomic-embed-text's context_length is 2048 tokens; real markdown (heavy on
# links/punctuation) tokenizes far less efficiently than plain prose --
# 8000 chars measurably 500'd with "input length exceeds context length"
# against real fixture content. 4000 verified safe across the most
# link-dense fixtures tested; also matches output_manager.py's existing
# 4000-char truncation for the extraction LLM call, reusing an established
# working precedent rather than picking a new number. Not tied to a
# specific model's exact limit -- deliberately conservative.
MAX_EMBED_CHARS = 4000


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def extract_headings(markdown: str) -> str:
    """Concatenated markdown ATX headings -- a candidate scoring unit much
    closer in length/specificity to a short intent string than a full
    page is, which is exactly the scale mismatch item 1 is worried about."""
    lines = [line.strip() for line in markdown.split("\n") if line.strip().startswith("#")]
    return "\n".join(lines)


def chunk_text(text: str, chunk_size: int = 2000, chunk_overlap: int = 0) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return splitter.split_text(text)


async def score_whole_page(intent_embedding: list[float], content: str, embed_fn: EmbedFn) -> float:
    """Candidate strategy 1: embed the page (truncated) as one unit."""
    page_embedding = await embed_fn(content[:MAX_EMBED_CHARS])
    return cosine_similarity(intent_embedding, page_embedding)


async def score_headings(intent_embedding: list[float], content: str, embed_fn: EmbedFn) -> float:
    """Candidate strategy 2: embed just the headings -- shorter, closer in
    scale to the intent string. Falls back to whole-page if the content
    has no markdown headings at all."""
    headings = extract_headings(content)
    if not headings:
        return await score_whole_page(intent_embedding, content, embed_fn)
    heading_embedding = await embed_fn(headings[:MAX_EMBED_CHARS])
    return cosine_similarity(intent_embedding, heading_embedding)


async def score_max_chunk(
    intent_embedding: list[float], content: str, embed_fn: EmbedFn, chunk_size: int = 2000
) -> float:
    """Candidate strategy 3: split into chunks, embed each, take the max
    similarity -- a long page can be mostly irrelevant to an intent even
    when one section is exactly what the user wants; averaging or
    whole-page scoring would dilute that section's own strong match."""
    chunks = chunk_text(content, chunk_size=chunk_size)
    if not chunks:
        return 0.0
    scores = [
        cosine_similarity(intent_embedding, await embed_fn(chunk[:MAX_EMBED_CHARS]))
        for chunk in chunks
    ]
    return max(scores)


# score_headings picked from a real measurement (see LESSONS_LEARNED.md
# #17), not the default guess (max-over-chunks) -- it was the only one of
# the three candidates that ranked a real API reference page above a
# marketing page for an intent specifically about that API, at 1 embed
# call/page instead of max_chunk's 5-13.
GateScoreFn = Callable[[str, str], Awaitable[float]]

# Returned when there's no intent to score against -- always above any
# reasonable threshold, so the gate is structurally a no-op rather than
# extract_worker needing a special case for "no intent given".
_NO_GATE_SCORE = 1.0


async def _no_gate_score_fn(url: str, content: str) -> float:
    return _NO_GATE_SCORE


def make_score_fn(intent_embedding: list[float] | None, embed_fn: EmbedFn) -> GateScoreFn:
    """intent_embedding=None means no intent was given -- the gate is off
    entirely, every page extracts, matching pipeline.py's ScoreFn shape
    (url, content) -> score with no special-casing needed in extract_worker."""
    if intent_embedding is None:
        return _no_gate_score_fn

    async def score_fn(url: str, content: str) -> float:
        return await score_headings(intent_embedding, content, embed_fn)

    return score_fn
