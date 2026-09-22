"""crawl/graph.py: discovery_graph() and path_tree(), against a small
synthetic frontier written directly via sqlite3 (no need for the async
Frontier/its lock -- these are pure offline reads over a finished DB, same
as a completed run would leave behind).

Graph (deliberately includes a real multi-parent case, a cross-host link,
and a capped max_pages leftover, to exercise every field the projections
report):

    https://x.test/            (depth 0, seed)
    +-- /a                     (depth 1)
    |   +-- /a/1               (depth 2)
    |   +-- /a/2               (depth 2)  <- also linked from /b (2nd parent)
    +-- /b                     (depth 1)
    |   +-- /a/2               (edge only, parent_url still /a on the row)
    +-- https://other.test/x   (depth 1, cross-host)
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from crawl.frontier import SCHEMA
from crawl.graph import discovery_graph, path_tree

SEED = "https://x.test/"
A = "https://x.test/a"
A1 = "https://x.test/a/1"
A2 = "https://x.test/a/2"
B = "https://x.test/b"
EXT = "https://other.test/x"


_PRE_EDGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier (
    url             TEXT PRIMARY KEY,
    host            TEXT NOT NULL,
    depth           INTEGER NOT NULL,
    section_label   TEXT,
    parent_url      TEXT,
    status          TEXT NOT NULL,
    relevance_score REAL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    not_before      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def _write_fixture_db(path: str, with_edges: bool = True) -> None:
    conn = sqlite3.connect(path)
    # with_edges=False simulates a DB written before discovery_edges
    # existed -- the table must be genuinely absent, not just empty, or
    # this wouldn't actually exercise the "predates the feature" path.
    conn.executescript(SCHEMA if with_edges else _PRE_EDGES_SCHEMA)
    now = "2026-01-01T00:00:00+00:00"
    rows = [
        (SEED, "x.test", 0, None, None, "done", 0.9),
        (A, "x.test", 1, None, SEED, "done", 0.8),
        (A1, "x.test", 2, None, A, "done", 0.7),
        (A2, "x.test", 2, None, A, "done", 0.6),  # first discoverer: a
        (B, "x.test", 1, None, SEED, "done", 0.5),
        (EXT, "other.test", 1, None, SEED, "skipped_extract", None),
    ]
    conn.executemany(
        "INSERT INTO frontier (url, host, depth, section_label, parent_url, status, "
        "relevance_score, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(*r, now, now) for r in rows],
    )
    if with_edges:
        edges = [
            (SEED, A, now), (SEED, B, now), (SEED, EXT, now),
            (A, A1, now), (A, A2, now),
            (B, A2, now),  # second discoverer of a2 -- b links to it too
        ]
        conn.executemany(
            "INSERT INTO discovery_edges (parent_url, child_url, discovered_at) VALUES (?, ?, ?)",
            edges,
        )
    conn.commit()
    conn.close()


class TestDiscoveryGraph(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "frontier.db")
        _write_fixture_db(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_whole_graph_node_fields(self):
        projection = discovery_graph(self.db_path)
        self.assertEqual(projection["type"], "discovery_graph")
        self.assertEqual(projection["seed_host"], "x.test")
        self.assertEqual(projection["node_count"], 6)
        self.assertTrue(projection["edges_tracked"])

        by_url = {n["url"]: n for n in projection["nodes"]}
        self.assertEqual(by_url[A1]["id"], A1)  # id == url, no surrogate key
        self.assertEqual(by_url[SEED]["child_count"], 3)  # a, b, ext
        self.assertEqual(by_url[SEED]["subtree_size"], 6)  # everything
        self.assertEqual(by_url[A]["subtree_size"], 3)  # a, a/1, a/2
        self.assertEqual(by_url[A]["child_count"], 2)

        # The whole point of discovery_edges: a/2's real inbound count (2:
        # from /a and /b) is visible even though frontier.parent_url only
        # ever recorded /a.
        self.assertEqual(by_url[A2]["parent_url"], A)
        self.assertEqual(by_url[A2]["inbound_count"], 2)
        self.assertEqual(by_url[A1]["inbound_count"], 1)
        self.assertEqual(projection["multi_inbound_link_count"], 1)

        self.assertTrue(by_url[EXT]["cross_host"])
        self.assertFalse(by_url[A]["cross_host"])
        self.assertEqual(by_url[EXT]["host"], "other.test")

    def test_edges_not_tracked_reports_none_not_zero(self):
        """A frontier.db written before discovery_edges existed must not
        report multi_inbound_link_count as a misleadingly confident 0."""
        db_path = str(Path(self._tmp.name) / "no_edges.db")
        _write_fixture_db(db_path, with_edges=False)
        projection = discovery_graph(db_path)
        self.assertFalse(projection["edges_tracked"])
        self.assertIsNone(projection["multi_inbound_link_count"])
        # Without edge data, inbound_count falls back to "1 if it has a
        # recorded parent, else 0" -- an honest lower bound, not a lie.
        by_url = {n["url"]: n for n in projection["nodes"]}
        self.assertEqual(by_url[A2]["inbound_count"], 1)
        self.assertEqual(by_url[SEED]["inbound_count"], 0)

    def test_scoped_subtree_query_matches_root_and_descendants_only(self):
        projection = discovery_graph(self.db_path, root_url=A)
        urls = {n["url"] for n in projection["nodes"]}
        self.assertEqual(urls, {A, A1, A2})
        self.assertEqual(projection["node_count"], 3)

    def test_scoped_query_with_max_depth_prunes_display_but_not_subtree_size(self):
        projection = discovery_graph(self.db_path, root_url=SEED, max_depth=1)
        urls = {n["url"] for n in projection["nodes"]}
        self.assertEqual(urls, {SEED, A, B, EXT})  # depth-2 nodes (a/1, a/2) cut
        by_url = {n["url"]: n for n in projection["nodes"]}
        # a's subtree_size still reflects a/1 + a/2, even though they're
        # not in this response -- what a UI needs to show "2 more below".
        self.assertEqual(by_url[A]["subtree_size"], 3)
        self.assertEqual(by_url[A]["child_count"], 2)

    def test_unknown_root_url_raises(self):
        with self.assertRaises(ValueError):
            discovery_graph(self.db_path, root_url="https://x.test/does-not-exist")


class TestPathTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "frontier.db")
        _write_fixture_db(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_host_urls_grouped_by_path_segment(self):
        projection = path_tree(self.db_path)
        self.assertEqual(projection["seed_host"], "x.test")
        tree = projection["tree"]
        self.assertIn("a", tree["children"])
        a_node = tree["children"]["a"]
        self.assertEqual(a_node["url_node"]["url"], A)
        self.assertEqual(a_node["url_node"]["child_count"], 2)  # 1, 2
        self.assertEqual(a_node["url_node"]["subtree_size"], 3)  # a, a/1, a/2
        self.assertIn("1", a_node["children"])
        self.assertIn("2", a_node["children"])

    def test_cross_host_urls_reported_flat_not_forced_into_tree(self):
        projection = path_tree(self.db_path)
        tree = projection["tree"]
        self.assertNotIn("other.test", str(tree))  # not smuggled into the same-host tree
        cross = {n["url"]: n for n in projection["cross_host_nodes"]}
        self.assertIn(EXT, cross)
        self.assertTrue(cross[EXT]["cross_host"])

    def test_prefix_scoped_query(self):
        projection = path_tree(self.db_path, root_prefix="https://x.test/a")
        self.assertEqual(projection["node_count"], 3)  # a, a/1, a/2
        tree = projection["tree"]
        self.assertIn("a", tree["children"])
        self.assertNotIn("b", tree["children"])

    def test_max_depth_prunes_tree_children(self):
        projection = path_tree(self.db_path, max_depth=1)
        a_node = projection["tree"]["children"]["a"]
        self.assertEqual(a_node["children"], {})  # pruned below depth 1
        # subtree_size/child_count still computed from the unpruned set
        self.assertEqual(a_node["url_node"]["subtree_size"], 3)
        self.assertEqual(a_node["url_node"]["child_count"], 2)

    def test_max_depth_with_root_prefix_is_relative_to_the_scope_root_not_the_domain_root(self):
        """Real site regression: a crawl root several segments deep (e.g.
        /en/stable/) must not make every root_prefix-scoped query look
        flat just because max_depth is small -- confirmed against a real
        docs.manim.community map where /en/stable collapsed to one node
        at max_depth=2 before this was fixed (see LESSONS_LEARNED #59)."""
        db_path = str(Path(self._tmp.name) / "deep.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        now = "2026-01-01T00:00:00+00:00"
        deep_seed = "https://x.test/en/stable/"
        deep_child = "https://x.test/en/stable/reference/foo"
        rows = [
            (deep_seed, "x.test", 0, None, None, "mapped", None),
            (deep_child, "x.test", 1, None, deep_seed, "mapped", None),
        ]
        conn.executemany(
            "INSERT INTO frontier (url, host, depth, section_label, parent_url, status, "
            "relevance_score, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*r, now, now) for r in rows],
        )
        conn.commit()
        conn.close()

        projection = path_tree(db_path, root_prefix=deep_seed, max_depth=1)
        # Walk down to the scope root (x.test -> en -> stable) and confirm
        # its OWN one level of children survived pruning.
        node = projection["tree"]["children"]["en"]["children"]["stable"]
        self.assertIn("reference", node["children"])

    def test_multi_inbound_link_count_matches_discovery_graph(self):
        projection = path_tree(self.db_path)
        self.assertEqual(projection["multi_inbound_link_count"], 1)  # a/2


class TestScaleAgainstRealPhase2Frontier(unittest.TestCase):
    """Item 4's scale check: both projections against the real 413-page /
    2039-row Phase 2 frontier.db, offline, no network. Skips cleanly if
    that file isn't present (e.g. a fresh checkout without the data/
    directory) rather than failing the suite on missing fixture data."""

    PHASE2_DB = str(Path(__file__).resolve().parent.parent / "data" / "run" / "frontier.db")

    def test_builds_without_error_and_reports_timing(self):
        if not Path(self.PHASE2_DB).exists():
            self.skipTest("data/run/frontier.db not present in this checkout")
        import time

        t0 = time.perf_counter()
        disc = discovery_graph(self.PHASE2_DB)
        disc_elapsed = time.perf_counter() - t0

        t0 = time.perf_counter()
        tree = path_tree(self.PHASE2_DB)
        tree_elapsed = time.perf_counter() - t0

        self.assertEqual(disc["node_count"], 2039)
        # edges_tracked reflects whether THIS real file's discovery_edges
        # table exists right now -- it was False when this test was
        # written (the file predated that table) but flipped True at some
        # point since, because Frontier.open() creates the table via
        # CREATE TABLE IF NOT EXISTS on every open, including a later
        # session's read against this same file. Asserting a fixed
        # historical value here was asserting a fact about this ambient,
        # mutable fixture's past, not a real invariant of the code -- it
        # was always going to go stale the next time anything touched this
        # file. The actual invariant crawl/graph.py guarantees (see
        # discovery_graph: multi_inbound = ... if edges_tracked else None)
        # is that the two fields agree; assert THAT instead, which holds
        # regardless of which state this file is in today AND still fails
        # if a future change breaks the real relationship between them.
        self.assertEqual(disc["edges_tracked"], disc["multi_inbound_link_count"] is not None)
        print(
            f"\n[scale check] discovery_graph: {disc_elapsed:.3f}s, "
            f"path_tree: {tree_elapsed:.3f}s, node_count={disc['node_count']}"
        )


if __name__ == "__main__":
    unittest.main()
