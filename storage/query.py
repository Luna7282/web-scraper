"""Query the Chroma index: embed a question, search child chunks, return
the parent text from metadata -- this is what makes the RAG index stop
being write-only (ROADMAP.md #3). Also the only place embedding-model
identity gets checked at query time (verify_embedding_identity), so a
mismatched model fails loudly instead of returning confidently wrong
nearest-neighbor results.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from storage.chunk_store import verify_embedding_identity

EmbedFn = Callable[[str], Awaitable[list[float]]]


@dataclass(frozen=True)
class QueryResult:
    child_text: str  # the actual chunk that matched -- useful for seeing WHY it matched
    parent_text: str  # the full context returned to the caller
    sources: list[str]
    distance: float


async def query_chunks(
    collection, embed_fn: EmbedFn, question: str, embedding_model: str, embedding_dim: int, k: int = 5
) -> list[QueryResult]:
    """Raises EmbeddingIdentityMismatch (loudly, not a warning) if this
    collection was built with a different embedding model/dimensionality
    than the one about to be used to embed the query."""
    verify_embedding_identity(collection, embedding_model, embedding_dim)

    query_embedding = await embed_fn(question)
    results = collection.query(query_embeddings=[query_embedding], n_results=k)

    out = []
    ids = results["ids"][0] if results["ids"] else []
    for i in range(len(ids)):
        metadata = results["metadatas"][0][i]
        out.append(QueryResult(
            child_text=results["documents"][0][i],
            parent_text=metadata.get("parent_text", ""),
            sources=metadata.get("sources", []),
            distance=results["distances"][0][i],
        ))
    return out


def format_query_results(results: list[QueryResult]) -> str:
    if not results:
        return "No results."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"--- Result {i}  (distance={r.distance:.4f}) ---")
        lines.append(f"Sources: {', '.join(r.sources)}")
        lines.append(f"Matched chunk: {r.child_text}")
        lines.append(f"Parent text: {r.parent_text}")
        lines.append("")
    return "\n".join(lines)
