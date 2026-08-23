"""Two read-only projections over a frontier DB -- discovery graph (edges
are parent_url, the literal shape the crawl found things in) and path tree
(nodes grouped by URL path segments, the shape a person expects a site to
look like). Both are pure reads: a fresh, short-lived sqlite3 connection
per call, never the async Frontier/its lock -- same "doesn't touch the
pipeline" discipline as progress_display.py and crawl/live_status.py, so
these are safe to run against a frontier.db belonging to a crawl that's
still running (WAL mode, set at Frontier.open() time, allows a concurrent
reader).

A real site is a graph, not a tree: frontier.parent_url only ever records
the *first* discoverer of a URL (INSERT OR IGNORE) -- a second parent
linking to an already-discovered page leaves no trace there. That's what
discovery_edges (see crawl/frontier.py's SCHEMA) exists to fix, going
forward: every (parent, child) pair ever passed to insert_children is
recorded there, even when the frontier-row insert itself was a no-op. A
frontier.db written before that table existed has none of this -- its
edges_tracked comes back False and multi_inbound_link_count is reported as
None rather than a misleading 0. There's no way to reconstruct those
missing edges after the fact (the content that would let us re-derive them
was never persisted either); the honest answer for an old DB is "unknown,"
not a guess.

Node identity: `url` (the frontier PRIMARY KEY) is already stable across
polls of the same frontier.db -- both projections and crawl/live_status.py
use it as-is for `id`, rather than inventing a surrogate key.
"""
from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlparse

EDGES_NOTE = (
    "discovery graph edges are parent_url -- the first discoverer only. "
    "A page linked from multiple places has only one edge here; see "
    "inbound_count per node and multi_inbound_link_count for what that "
    "hides. edges_tracked=false means this frontier.db predates "
    "discovery_edges and the real inbound count for it is unrecoverable, "
    "not zero."
)


def _open_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _inbound_counts(conn: sqlite3.Connection, urls: set[str] | None = None) -> tuple[dict[str, int], bool]:
    """(child_url -> distinct inbound parent count, edges_tracked). Scoped
    to `urls` when given (subtree-scoped queries never need fan-in data
    for the whole DB, just their own node set) -- cheap either way since
    idx_edges_child backs the child_url side."""
    if not _has_table(conn, "discovery_edges"):
        return {}, False
    if urls is None:
        cur = conn.execute(
            "SELECT child_url, COUNT(DISTINCT parent_url) AS n FROM discovery_edges GROUP BY child_url"
        )
        return {row["child_url"]: row["n"] for row in cur.fetchall()}, True
    counts: dict[str, int] = {}
    # Chunked IN() -- SQLite's default host-parameter limit is 999.
    urls_list = list(urls)
    for i in range(0, len(urls_list), 900):
        chunk = urls_list[i:i + 900]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"SELECT child_url, COUNT(DISTINCT parent_url) AS n FROM discovery_edges "
            f"WHERE child_url IN ({placeholders}) GROUP BY child_url",
            chunk,
        )
        counts.update({row["child_url"]: row["n"] for row in cur.fetchall()})
    return counts, True


def _subtree_sizes_and_child_counts(rows: list[sqlite3.Row]) -> tuple[dict[str, int], dict[str, int]]:
    """One linear pass over (url, parent_url) pairs -- iterative post-order
    (not recursive: a real crawl's parent chain can run deeper than
    Python's default recursion limit), building child_count (direct) and
    subtree_size (all descendants) per node at the same time."""
    children: dict[str, list[str]] = {}
    all_urls = set()
    for row in rows:
        all_urls.add(row["url"])
        if row["parent_url"] is not None:
            children.setdefault(row["parent_url"], []).append(row["url"])

    child_count = {u: len(children.get(u, [])) for u in all_urls}
    subtree_size: dict[str, int] = {}
    roots = [u for u in all_urls if u not in {c for kids in children.values() for c in kids}] or list(all_urls)
    # Roots not in the row set itself (parent outside the scoped window)
    # still work: their children are walked from `children`, they're just
    # never assigned a subtree_size of their own, which is correct -- they
    # aren't one of `rows`.
    for start in roots:
        stack = [(start, False)]
        while stack:
            node, processed = stack.pop()
            if processed:
                subtree_size[node] = 1 + sum(subtree_size.get(c, 0) for c in children.get(node, []))
                continue
            if node in subtree_size:
                continue
            stack.append((node, True))
            for c in children.get(node, []):
                stack.append((c, False))
    return subtree_size, child_count


def _node_dict(row: sqlite3.Row, seed_host: str | None, inbound: dict, child_count: dict, subtree_size: dict) -> dict:
    host = row["host"]
    return {
        "id": row["url"],
        "url": row["url"],
        "parent_url": row["parent_url"],
        "status": row["status"],
        "depth": row["depth"],
        "relevance_score": row["relevance_score"],
        "host": host,
        "cross_host": seed_host is not None and host != seed_host,
        "child_count": child_count.get(row["url"], 0),
        "subtree_size": subtree_size.get(row["url"], 1),
        "inbound_count": inbound.get(row["url"], 1 if row["parent_url"] is not None else 0),
    }


def _seed_host(conn: sqlite3.Connection, fallback_url: str | None) -> str | None:
    cur = conn.execute("SELECT host FROM frontier WHERE depth = 0 LIMIT 1")
    row = cur.fetchone()
    if row is not None:
        return row["host"]
    return urlparse(fallback_url).netloc.lower() if fallback_url else None


def discovery_graph(db_path: str, root_url: str | None = None, max_depth: int | None = None) -> dict:
    """Whole-graph dump (root_url=None) or a subtree-scoped window (given
    node + up to max_depth hops of descendants) -- the latter is what lets
    a UI load/render one region instead of the whole graph at once.
    child_count/subtree_size on every returned node (including ones right
    at the depth cutoff) reflect the TRUE full subtree, not just what's
    included in this response -- computed from the full descendant set of
    root_url (bounded to that subtree, not the whole DB), so a UI can show
    "12 more below" on a collapsed boundary node without a second query.
    """
    conn = _open_ro(db_path)
    try:
        if root_url is None:
            all_rows = conn.execute(
                "SELECT url, host, depth, status, relevance_score, parent_url FROM frontier"
            ).fetchall()
            scope_rows = all_rows
        else:
            cur = conn.execute(
                """
                WITH RECURSIVE subtree(url) AS (
                    SELECT url FROM frontier WHERE url = ?
                    UNION ALL
                    SELECT f.url FROM frontier f JOIN subtree s ON f.parent_url = s.url
                )
                SELECT f.url, f.host, f.depth, f.status, f.relevance_score, f.parent_url
                FROM frontier f WHERE f.url IN (SELECT url FROM subtree)
                """,
                (root_url,),
            )
            scope_rows = cur.fetchall()
            if not scope_rows:
                raise ValueError(f"no such url in this frontier: {root_url!r}")

        subtree_size, child_count = _subtree_sizes_and_child_counts(scope_rows)
        seed_host = _seed_host(conn, root_url)
        url_set = {r["url"] for r in scope_rows}
        inbound, edges_tracked = _inbound_counts(conn, url_set)

        if root_url is not None and max_depth is not None:
            root_depth = next(r["depth"] for r in scope_rows if r["url"] == root_url)
            returned_rows = [r for r in scope_rows if r["depth"] - root_depth <= max_depth]
        else:
            returned_rows = scope_rows

        nodes = [_node_dict(r, seed_host, inbound, child_count, subtree_size) for r in returned_rows]
        multi_inbound = sum(1 for n in inbound.values() if n > 1) if edges_tracked else None

        return {
            "type": "discovery_graph",
            "seed_host": seed_host,
            "root_url": root_url,
            "max_depth": max_depth,
            "node_count": len(nodes),
            "edges_tracked": edges_tracked,
            "multi_inbound_link_count": multi_inbound,
            "note": EDGES_NOTE,
            "nodes": nodes,
        }
    finally:
        conn.close()


def _path_segments(url: str) -> list[str]:
    path = urlparse(url).path or "/"
    return [p for p in path.split("/") if p]


def _build_path_tree(rows: list[sqlite3.Row], seed_host: str, inbound: dict) -> tuple[dict, list[dict]]:
    by_url = {r["url"]: r for r in rows}
    same_host_urls = {u for u, r in by_url.items() if r["host"] == seed_host}
    cross_host_rows = [r for r in rows if r["host"] != seed_host]

    root: dict = {"segment": "", "path": "/", "url_node": None, "children": {}}
    node_at_path: dict[str, dict] = {"/": root}

    for url in same_host_urls:
        segments = _path_segments(url)
        cur = root
        cur_path = "/"
        for seg in segments:
            cur_path = cur_path.rstrip("/") + "/" + seg
            if seg not in cur["children"]:
                child = {"segment": seg, "path": cur_path, "url_node": None, "children": {}}
                cur["children"][seg] = child
                node_at_path[cur_path] = child
            cur = cur["children"][seg]
        row = by_url[url]
        cur["url_node"] = {
            "id": url, "url": url, "status": row["status"], "depth": row["depth"],
            "relevance_score": row["relevance_score"], "host": row["host"], "cross_host": False,
            "inbound_count": inbound.get(url, 0),
        }

    def annotate(node: dict) -> int:
        """Post-order: fills child_count/subtree_size on any url_node
        present at this path, returns this path's own subtree url-count."""
        total = 1 if node["url_node"] is not None else 0
        for child in node["children"].values():
            total += annotate(child)
        if node["url_node"] is not None:
            node["url_node"]["child_count"] = len(node["children"])
            node["url_node"]["subtree_size"] = total
        return total

    annotate(root)

    cross_host_nodes = [
        {
            "id": r["url"], "url": r["url"], "status": r["status"], "depth": r["depth"],
            "relevance_score": r["relevance_score"], "host": r["host"], "cross_host": True,
            "child_count": 0, "subtree_size": 1, "inbound_count": inbound.get(r["url"], 0),
        }
        for r in cross_host_rows
    ]
    return root, cross_host_nodes


def _prune_depth(node: dict, remaining: int | None) -> None:
    if remaining is None:
        return
    if remaining <= 0:
        node["children"] = {}
        return
    for child in node["children"].values():
        _prune_depth(child, remaining - 1)


def path_tree(db_path: str, root_prefix: str | None = None, max_depth: int | None = None) -> dict:
    """Groups nodes by URL path segments, independent of discovery order --
    "what people expect a site to look like", as opposed to discovery_graph's
    honest-but-messy first-discoverer shape. Off-seed-host URLs aren't
    forced into a path hierarchy for a site this crawl didn't actually
    explore -- they're reported flat in cross_host_nodes instead, each
    still carrying full node fields.

    root_prefix scopes to URLs under that path prefix (subtree-scoped
    loading, same purpose as discovery_graph's root_url); max_depth caps
    how many path-segment levels deep the returned tree goes below that
    scope root -- but subtree_size/child_count on the boundary nodes still
    reflect the true (unpruned) count, computed before pruning.
    """
    conn = _open_ro(db_path)
    try:
        seed_host = _seed_host(conn, None)
        if seed_host is None:
            return {
                "type": "path_tree", "seed_host": None, "root_prefix": root_prefix,
                "max_depth": max_depth, "node_count": 0, "edges_tracked": False,
                "multi_inbound_link_count": None, "note": EDGES_NOTE,
                "tree": {"segment": "", "path": "/", "url_node": None, "children": {}},
                "cross_host_nodes": [],
            }

        if root_prefix is not None:
            cur = conn.execute(
                "SELECT url, host, depth, status, relevance_score, parent_url FROM frontier "
                "WHERE url LIKE ? ESCAPE '\\'",
                (_escape_like(root_prefix) + "%",),
            )
        else:
            cur = conn.execute("SELECT url, host, depth, status, relevance_score, parent_url FROM frontier")
        rows = cur.fetchall()

        url_set = {r["url"] for r in rows}
        inbound, edges_tracked = _inbound_counts(conn, url_set)
        tree, cross_host_nodes = _build_path_tree(rows, seed_host, inbound)
        # max_depth is relative to root_prefix's own node, not the domain
        # root -- a real site's crawl root is usually itself several path
        # segments deep (locale/version prefixes like /en/stable/), and
        # pruning from "/" made every such site look like one flat node at
        # low max_depth regardless of root_prefix (confirmed against a real
        # docs.manim.community map: /en/stable collapses depth 1-2 to a
        # single node; the real branching -- reference/, changelog/,
        # tutorials/, etc -- only starts one level past the scope root).
        scope_node = tree
        if root_prefix is not None:
            for seg in _path_segments(root_prefix):
                if seg not in scope_node["children"]:
                    break
                scope_node = scope_node["children"][seg]
        _prune_depth(scope_node, max_depth)

        multi_inbound = sum(1 for n in inbound.values() if n > 1) if edges_tracked else None
        return {
            "type": "path_tree",
            "seed_host": seed_host,
            "root_prefix": root_prefix,
            "max_depth": max_depth,
            "node_count": len(rows),
            "edges_tracked": edges_tracked,
            "multi_inbound_link_count": multi_inbound,
            "note": EDGES_NOTE,
            "tree": tree,
            "cross_host_nodes": cross_host_nodes,
        }
    finally:
        conn.close()


def to_json(projection: dict) -> str:
    return json.dumps(projection, indent=2)
