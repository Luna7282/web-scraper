"""Selects which chunk(s) of a page's stripped markdown get sent through
Q&A extraction -- the fix for the truncation problem found in step 7's
cross-site check (LESSONS_LEARNED.md #25, ROADMAP.md #21): a single
content[:4000] call only ever sees a page's opening slice (16% of a
24,000-char page), so real content past that point was never extractable
no matter how good chrome-stripping or the prompt are.

Three strategies (config.EXTRACTION_STRATEGY):
- first_n_chars: one unit, content[:max_chars] -- the old behavior, kept
  as an explicit opt-in (cheapest: 1 call/page) rather than removed.
- per_chunk: every parent-sized chunk becomes its own extraction call --
  complete (nothing beyond the old window is skipped) but costs N calls
  per page instead of 1. The new default.
- top_k_chunks_by_relevance: chunk, embed each chunk, keep only the
  top_k most similar to the intent embedding. Cheaper than per_chunk on
  a large crawl, but needs an intent to rank against -- with no intent
  there's no relevance signal to select by, so this falls back to
  per_chunk rather than picking an arbitrary subset.

Reuses relevance.py's chunk_text/cosine_similarity instead of a new
chunker -- these are the same parent-sized units (config.PARENT_CHUNK_SIZE/
_OVERLAP) already used for Chroma, so a unit here is never larger than
what embedding already handles safely.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from config import (
    EXTRACTION_TOP_K,
    MAX_EXTRACT_CHARS,
    PARENT_CHUNK_OVERLAP,
    PARENT_CHUNK_SIZE,
    ExtractionStrategy,
)
from content.relevance import chunk_text, cosine_similarity

EmbedFn = Callable[[str], Awaitable[list[float]]]


async def select_extraction_units(
    content: str,
    strategy: ExtractionStrategy,
    *,
    embed_fn: EmbedFn | None = None,
    intent_embedding: list[float] | None = None,
    top_k: int = EXTRACTION_TOP_K,
    chunk_size: int = PARENT_CHUNK_SIZE,
    chunk_overlap: int = PARENT_CHUNK_OVERLAP,
    max_chars: int = MAX_EXTRACT_CHARS,
) -> list[str]:
    if strategy == ExtractionStrategy.FIRST_N_CHARS:
        return [content[:max_chars]]

    chunks = chunk_text(content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    if not chunks:
        return []

    if strategy == ExtractionStrategy.PER_CHUNK:
        return chunks

    if strategy == ExtractionStrategy.TOP_K_CHUNKS_BY_RELEVANCE:
        if embed_fn is None or intent_embedding is None:
            return chunks  # no relevance signal to rank by -- fall back to complete coverage
        scored = [
            (cosine_similarity(intent_embedding, await embed_fn(chunk[:max_chars])), chunk)
            for chunk in chunks
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [chunk for _, chunk in scored[:top_k]]

    raise ValueError(f"unknown extraction strategy: {strategy!r}")


def make_extraction_units_fn(
    strategy: ExtractionStrategy,
    *,
    embed_fn: EmbedFn | None = None,
    intent_embedding: list[float] | None = None,
    top_k: int = EXTRACTION_TOP_K,
) -> Callable[[str], Awaitable[list[str]]]:
    async def extraction_units_fn(content: str) -> list[str]:
        return await select_extraction_units(
            content, strategy, embed_fn=embed_fn, intent_embedding=intent_embedding, top_k=top_k,
        )
    return extraction_units_fn
