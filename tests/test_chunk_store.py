"""Offline tests for chunk_store.py -- chromadb.EphemeralClient() (no
persistence, no network) and a stub embed_fn (no real embedding calls).

Confirmed empirically before relying on it: separate EphemeralClient()
instances in the same process share collection state (not
per-instance-isolated the way the name suggests) -- so every test uses a
unique collection name rather than assuming a fresh client means a clean
slate.
"""
import unittest
import uuid

import chromadb

from storage.chunk_store import (
    normalize_chunk_text, chunk_id, split_into_parent_child_chunks,
    get_or_create_collection, verify_embedding_identity, EmbeddingIdentityMismatch,
    ChunkStore,
)


def _unique_name() -> str:
    return f"test_{uuid.uuid4().hex}"


class TestNormalizeChunkText(unittest.TestCase):
    def test_whitespace_only_difference_normalizes_identically(self):
        a = "The name for the Python project is manimations."
        b = "The name for the Python\n  project is   manimations.\n"
        self.assertEqual(normalize_chunk_text(a), normalize_chunk_text(b))

    def test_case_difference_normalizes_identically(self):
        self.assertEqual(normalize_chunk_text("Hello World"), normalize_chunk_text("hello world"))

    def test_trailing_punctuation_difference_normalizes_identically(self):
        self.assertEqual(normalize_chunk_text("some text."), normalize_chunk_text("some text"))
        self.assertEqual(normalize_chunk_text("a question?"), normalize_chunk_text("a question"))

    def test_genuinely_different_text_does_not_collide(self):
        self.assertNotEqual(normalize_chunk_text("circle radius"), normalize_chunk_text("square width"))


class TestChunkId(unittest.TestCase):
    def test_whitespace_variant_chunks_collide(self):
        a = "uv init manimations\ncd manimations\nuv add manim"
        b = "uv init manimations\n  cd manimations\n  uv add manim  "
        self.assertEqual(chunk_id(a), chunk_id(b))

    def test_different_content_does_not_collide(self):
        self.assertNotEqual(chunk_id("chunk one"), chunk_id("chunk two"))

    def test_stable_across_calls(self):
        text = "stable text"
        self.assertEqual(chunk_id(text), chunk_id(text))


class TestSplitIntoParentChildChunks(unittest.TestCase):
    def test_short_text_single_parent_single_child(self):
        pairs = split_into_parent_child_chunks("short", parent_size=2000, parent_overlap=200,
                                                child_size=400, child_overlap=200)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0], ("short", "short"))

    def test_every_child_carries_its_own_parent_text(self):
        text = "word " * 200  # long enough to need multiple parents/children
        pairs = split_into_parent_child_chunks(text, parent_size=300, parent_overlap=0,
                                                child_size=100, child_overlap=0)
        self.assertGreater(len(pairs), 1)
        for child, parent in pairs:
            self.assertIn(child.strip(), parent)


def make_stub_embed_fn():
    calls = []
    async def embed_fn(text: str) -> list[float]:
        calls.append(text)
        return [float(len(text)), 0.0]  # deterministic, content-dependent fake vector
    embed_fn.calls = calls
    return embed_fn


class TestEmbeddingIdentity(unittest.TestCase):
    def test_fresh_collection_passes(self):
        client = chromadb.EphemeralClient()
        collection = get_or_create_collection(client, _unique_name(), "nomic-embed-text", 768)
        self.assertEqual(collection.metadata["embedding_model"], "nomic-embed-text")

    def test_matching_identity_on_reopen_passes(self):
        client = chromadb.EphemeralClient()
        name = _unique_name()
        get_or_create_collection(client, name, "nomic-embed-text", 768)
        collection = get_or_create_collection(client, name, "nomic-embed-text", 768)
        self.assertIsNotNone(collection)

    def test_mismatched_model_raises_loudly(self):
        client = chromadb.EphemeralClient()
        name = _unique_name()
        get_or_create_collection(client, name, "nomic-embed-text", 768)
        with self.assertRaises(EmbeddingIdentityMismatch):
            get_or_create_collection(client, name, "text-embedding-3-small", 768)

    def test_mismatched_dim_raises_loudly(self):
        client = chromadb.EphemeralClient()
        name = _unique_name()
        get_or_create_collection(client, name, "nomic-embed-text", 768)
        with self.assertRaises(EmbeddingIdentityMismatch):
            get_or_create_collection(client, name, "nomic-embed-text", 1536)

    def test_verify_does_not_raise_on_collection_with_no_recorded_identity(self):
        client = chromadb.EphemeralClient()
        collection = client.get_or_create_collection(_unique_name())  # no metadata at all
        verify_embedding_identity(collection, "nomic-embed-text", 768)  # must not raise


class TestChunkStore(unittest.IsolatedAsyncioTestCase):
    async def test_new_chunk_gets_embedded_and_added(self):
        client = chromadb.EphemeralClient()
        collection = get_or_create_collection(client, _unique_name(), "fake-model", 2)
        embed_fn = make_stub_embed_fn()
        store = ChunkStore(collection, embed_fn)

        cid, embedded = await store.add_or_merge_chunk("some chunk text", "https://x.com/a", "parent text")
        self.assertTrue(embedded)
        self.assertEqual(len(embed_fn.calls), 1)
        got = collection.get(ids=[cid])
        self.assertEqual(got["metadatas"][0]["sources"], ["https://x.com/a"])
        self.assertEqual(got["metadatas"][0]["parent_text"], "parent text")

    async def test_cross_page_duplicate_chunk_merges_without_reembedding(self):
        client = chromadb.EphemeralClient()
        collection = get_or_create_collection(client, _unique_name(), "fake-model", 2)
        embed_fn = make_stub_embed_fn()
        store = ChunkStore(collection, embed_fn)

        text = "identical install instructions repeated on every page"
        cid1, embedded1 = await store.add_or_merge_chunk(text, "https://x.com/linux", "parent")
        cid2, embedded2 = await store.add_or_merge_chunk(text, "https://x.com/macos", "parent")

        self.assertEqual(cid1, cid2)
        self.assertTrue(embedded1)
        self.assertFalse(embedded2)  # no second embed call
        self.assertEqual(len(embed_fn.calls), 1)  # confirms no re-embedding happened

        got = collection.get(ids=[cid1])
        self.assertEqual(set(got["metadatas"][0]["sources"]), {"https://x.com/linux", "https://x.com/macos"})
        self.assertEqual(collection.count(), 1)  # one vector, not two

    async def test_same_source_added_twice_does_not_duplicate_in_sources_list(self):
        client = chromadb.EphemeralClient()
        collection = get_or_create_collection(client, _unique_name(), "fake-model", 2)
        embed_fn = make_stub_embed_fn()
        store = ChunkStore(collection, embed_fn)

        text = "some content"
        await store.add_or_merge_chunk(text, "https://x.com/a", "parent")
        await store.add_or_merge_chunk(text, "https://x.com/a", "parent")  # same source again

        cid = chunk_id(text)
        got = collection.get(ids=[cid])
        self.assertEqual(got["metadatas"][0]["sources"], ["https://x.com/a"])

    async def test_whitespace_variant_across_pages_still_merges(self):
        client = chromadb.EphemeralClient()
        collection = get_or_create_collection(client, _unique_name(), "fake-model", 2)
        embed_fn = make_stub_embed_fn()
        store = ChunkStore(collection, embed_fn)

        cid1, _ = await store.add_or_merge_chunk(
            "uv init manimations\ncd manimations\nuv add manim", "https://x.com/linux", "p"
        )
        cid2, embedded2 = await store.add_or_merge_chunk(
            "uv init manimations\n  cd manimations\n  uv add manim  ", "https://x.com/macos", "p"
        )
        self.assertEqual(cid1, cid2)
        self.assertFalse(embedded2)


if __name__ == "__main__":
    unittest.main()
