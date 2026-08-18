"""Offline tests for query.py -- chromadb.EphemeralClient(), stub embed_fn,
no real embeddings, no network."""
import unittest
import uuid

import chromadb

from chunk_store import get_or_create_collection, EmbeddingIdentityMismatch
from query import query_chunks, format_query_results, QueryResult


def _unique_name() -> str:
    return f"test_{uuid.uuid4().hex}"


class TestQueryChunks(unittest.IsolatedAsyncioTestCase):
    async def test_returns_parent_text_not_child_text(self):
        client = chromadb.EphemeralClient()
        collection = get_or_create_collection(client, _unique_name(), "fake-model", 2)
        collection.add(
            ids=["c1"], embeddings=[[1.0, 0.0]],
            documents=["short child snippet"],
            metadatas=[{"parent_text": "The FULL parent context, much longer.", "sources": ["https://x.com/a"]}],
        )

        async def embed_fn(text):
            return [1.0, 0.0]

        results = await query_chunks(collection, embed_fn, "a question", "fake-model", 2, k=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].child_text, "short child snippet")
        self.assertEqual(results[0].parent_text, "The FULL parent context, much longer.")
        self.assertEqual(results[0].sources, ["https://x.com/a"])

    async def test_results_ordered_by_distance(self):
        client = chromadb.EphemeralClient()
        collection = get_or_create_collection(client, _unique_name(), "fake-model", 2)
        collection.add(
            ids=["far", "near"],
            embeddings=[[0.0, 1.0], [0.99, 0.01]],
            documents=["far doc", "near doc"],
            metadatas=[{"parent_text": "far parent", "sources": ["u1"]}, {"parent_text": "near parent", "sources": ["u2"]}],
        )

        async def embed_fn(text):
            return [1.0, 0.0]

        results = await query_chunks(collection, embed_fn, "q", "fake-model", 2, k=2)
        self.assertEqual(results[0].parent_text, "near parent")
        self.assertLess(results[0].distance, results[1].distance)

    async def test_mismatched_embedding_identity_raises_not_returns_wrong_results(self):
        client = chromadb.EphemeralClient()
        collection = get_or_create_collection(client, _unique_name(), "nomic-embed-text", 768)

        async def embed_fn(text):
            return [0.0] * 1536

        with self.assertRaises(EmbeddingIdentityMismatch):
            await query_chunks(collection, embed_fn, "q", "text-embedding-3-small", 1536)

    async def test_empty_collection_returns_empty_list(self):
        client = chromadb.EphemeralClient()
        collection = get_or_create_collection(client, _unique_name(), "fake-model", 2)

        async def embed_fn(text):
            return [1.0, 0.0]

        results = await query_chunks(collection, embed_fn, "q", "fake-model", 2)
        self.assertEqual(results, [])


class TestFormatQueryResults(unittest.TestCase):
    def test_empty_results(self):
        self.assertIn("No results", format_query_results([]))

    def test_formats_parent_text_and_sources(self):
        results = [QueryResult(child_text="short child snippet", parent_text="Some parent context.",
                                sources=["https://x.com/a"], distance=0.12)]
        formatted = format_query_results(results)
        self.assertIn("short child snippet", formatted)
        self.assertIn("Some parent context.", formatted)
        self.assertIn("https://x.com/a", formatted)
        self.assertIn("0.1200", formatted)


if __name__ == "__main__":
    unittest.main()
