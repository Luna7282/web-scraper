"""Chroma-backed chunk storage.

Two things this exists to get right, both corrections from earlier
mistakes documented in ROADMAP.md/LESSONS_LEARNED.md:

1. Chunk IDs hash normalized TEXT ALONE, not url+text (ROADMAP #6's
   correction) -- the same real content repeated verbatim across
   different pages (the "install instructions on every OS's page" case
   from LESSONS_LEARNED #4/#9) must collide onto the same Chroma id and
   upsert, not produce N near-identical competing vectors.
2. Chroma's own upsert() REPLACES metadata wholesale -- it does not merge
   a "sources" list. Confirmed empirically before writing this: add() with
   a duplicate id silently no-ops (the second call's metadata is dropped
   entirely), and upsert() with a duplicate id overwrites metadata with
   exactly what's passed. Appending a new source to an existing chunk's
   sources list requires an explicit read-merge-write (ChunkStore.
   add_or_merge_chunk below) -- there is no shortcut around this.
"""
from __future__ import annotations

import hashlib
from typing import Awaitable, Callable

from content.relevance import chunk_text as _split_text

EmbedFn = Callable[[str], Awaitable[list[float]]]

_TRAILING_PUNCT = ".,;:!?"


def normalize_chunk_text(text: str) -> str:
    """Strict normalization so semantically-identical chunks collide on
    the same hash even when whitespace, case, or trailing punctuation
    differ between the two pages they came from -- exactly the kind of
    difference that would otherwise silently defeat the collision this
    exists for. Collapses all whitespace (including newlines) to single
    spaces, strips both ends, lowercases, and strips trailing punctuation.
    """
    normalized = " ".join(text.split())
    normalized = normalized.lower()
    normalized = normalized.rstrip(_TRAILING_PUNCT)
    return normalized


def chunk_id(text: str) -> str:
    """sha256 of the normalized text alone -- see module docstring #1."""
    return hashlib.sha256(normalize_chunk_text(text).encode("utf-8")).hexdigest()


def split_into_parent_child_chunks(
    markdown: str, parent_size: int, parent_overlap: int, child_size: int, child_overlap: int
) -> list[tuple[str, str]]:
    """Returns (child_text, parent_text) pairs -- parent chunks split
    first, each parent re-split into children, matching the original
    parent/child design (output_manager.py) with sizes/overlap now
    explicit parameters instead of library defaults (see config.py,
    LESSONS_LEARNED.md #19)."""
    pairs = []
    for parent in _split_text(markdown, chunk_size=parent_size, chunk_overlap=parent_overlap):
        for child in _split_text(parent, chunk_size=child_size, chunk_overlap=child_overlap):
            pairs.append((child, parent))
    return pairs


class EmbeddingIdentityMismatch(Exception):
    pass


def get_or_create_collection(client, name: str, embedding_model: str, embedding_dim: int):
    """Records embedding model identity in collection metadata at
    creation. If the collection already exists, chromadb preserves its
    ORIGINAL metadata rather than overwriting it (confirmed empirically),
    so comparing the returned collection's metadata against this run's
    model/dim reliably catches a mismatch -- not just on first creation."""
    collection = client.get_or_create_collection(
        name, metadata={"embedding_model": embedding_model, "embedding_dim": embedding_dim}
    )
    verify_embedding_identity(collection, embedding_model, embedding_dim)
    return collection


def verify_embedding_identity(collection, embedding_model: str, embedding_dim: int) -> None:
    """Loud failure (raises) on mismatch -- not a warning that scrolls
    past a long crawl's output. A silent mismatch means query-time
    vectors get compared against index-time vectors from an incompatible
    embedding space, which produces confidently wrong nearest-neighbor
    results, not an error -- exactly the failure mode that must not be
    optional or downgradeable to a log line."""
    metadata = collection.metadata or {}
    recorded_model = metadata.get("embedding_model")
    recorded_dim = metadata.get("embedding_dim")
    if recorded_model is None and recorded_dim is None:
        return  # freshly created this call; nothing to compare against yet
    if recorded_model != embedding_model or recorded_dim != embedding_dim:
        raise EmbeddingIdentityMismatch(
            f"Collection {collection.name!r} was built with "
            f"embedding_model={recorded_model!r} embedding_dim={recorded_dim!r}, "
            f"but this run is using embedding_model={embedding_model!r} "
            f"embedding_dim={embedding_dim!r}. Querying or writing with a "
            f"mismatched embedding space produces confidently wrong results, "
            f"not an error -- use the matching model or a different "
            f"collection name, don't ignore this."
        )


class ChunkStore:
    def __init__(self, collection, embed_fn: EmbedFn):
        self._collection = collection
        self._embed_fn = embed_fn

    async def add_or_merge_chunk(self, text: str, source_url: str, parent_text: str) -> tuple[str, bool]:
        """Returns (chunk_id, embedded). embedded=False means the chunk's
        content-hash id already existed and only its sources list was
        updated via a metadata-only update() call -- no embedding call
        made, since content-identical chunks have identical embeddings by
        construction (same model, same normalized text -> same vector),
        so recomputing would be pure waste."""
        cid = chunk_id(text)
        existing = self._collection.get(ids=[cid])
        if existing["ids"]:
            sources = existing["metadatas"][0].get("sources", [])
            if source_url not in sources:
                merged_meta = dict(existing["metadatas"][0])
                merged_meta["sources"] = sources + [source_url]
                self._collection.update(ids=[cid], metadatas=[merged_meta])
            return cid, False

        embedding = await self._embed_fn(text)
        self._collection.add(
            ids=[cid],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{"sources": [source_url], "parent_text": parent_text}],
        )
        return cid, True
