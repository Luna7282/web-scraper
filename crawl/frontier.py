"""SQLite-backed crawl frontier: durable URL state, the visited-set, and the
in_flight/quiescence bookkeeping that decides when a run is actually done.

State machine (see CLAUDE.md for the full table): pending_score -> queued ->
in_progress -> done / failed / skipped_follow / skipped_extract / mapped.
done and skipped_extract both imply a relevance score was computed; failed
is the only one of those five that doesn't, which is why orphan resolution
keys off it alone (see _locked_mark_terminal). mapped means a page was
fetched with no scoring/extraction attempted at all (map mode -- see
CLAUDE.md) -- kept distinct from skipped_extract (which means the
relevance gate *did* run and reject the page) so a map run's status counts
don't read as a mass rejection.

cancelled is a sixth terminal status, reachable from pending_score/queued/
in_progress only (see cancel()) -- not part of the score/no-score split
above since it means "never processed", not an outcome of processing.

## Locking discipline (structurally enforced, not just documented)

Every method name starting with `_locked_` assumes `self._lock` is ALREADY
held by its caller and must never acquire it, and must never call a public
(non-`_locked_`) method -- asyncio.Lock is not reentrant, so doing either
deadlocks permanently, not slowly. Public methods acquire the lock, delegate
to a `_locked_*` method, and release.

Awaits ARE permitted inside the locked critical section -- but only for DB
calls on `self._conn` (the shared aiosqlite connection). Never a queue
operation, network call, LLM call, or sleep inside the lock: `content_queue`
and `results_queue` are bounded for backpressure, so `await queue.put()` can
block indefinitely; holding the lock across that would stall every other
worker, including the ones that would drain the queue and let it unblock --
a full deadlock, not just a slow path. This is why `put_content`/`put_results`
below explicitly release the lock before the `await queue.put()` call.
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import aiosqlite

from config import MAX_RETRIES, FOLLOW_GATE_EXEMPT_DEPTH

MIN_SQLITE_VERSION = (3, 35, 0)  # UPDATE ... RETURNING

SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier (
    url             TEXT PRIMARY KEY,
    host            TEXT NOT NULL,
    depth           INTEGER NOT NULL,
    section_label   TEXT,
    parent_url      TEXT,
    status          TEXT NOT NULL CHECK (status IN (
                        'pending_score', 'queued', 'in_progress',
                        'done', 'failed', 'skipped_follow', 'skipped_extract',
                        'mapped', 'cancelled'
                    )),
    relevance_score REAL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    not_before      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frontier_status ON frontier(status);
CREATE INDEX IF NOT EXISTS idx_frontier_parent ON frontier(parent_url);
CREATE INDEX IF NOT EXISTS idx_frontier_host   ON frontier(host);

-- Every (parent, child) discovery edge, not just the first-discoverer
-- winner frontier.parent_url keeps (INSERT OR IGNORE means a second
-- parent linking to an already-inserted child is otherwise silently
-- lost). Additive on an existing DB opened by a newer version of this
-- module -- CREATE TABLE IF NOT EXISTS, no migration of old rows,
-- because there's nothing to migrate: a DB written before this table
-- existed genuinely never recorded those extra edges. See CLAUDE.md /
-- crawl/graph.py for what reads this (real inbound-link-count per node).
CREATE TABLE IF NOT EXISTS discovery_edges (
    parent_url    TEXT NOT NULL,
    child_url     TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (parent_url, child_url)
);
CREATE INDEX IF NOT EXISTS idx_edges_child ON discovery_edges(child_url);

-- Single-row control flags, readable/writable across process boundaries
-- (the server pausing a crawl is a DIFFERENT OS process than the one
-- running it -- an in-memory flag on this Frontier instance would be
-- invisible to the other process's Frontier instance; the DB is the only
-- thing both sides actually share). id=1 enforced so there is never more
-- than one row to disagree with itself.
CREATE TABLE IF NOT EXISTS control (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO control (id, paused) VALUES (1, 0);
"""

# The three statuses nothing has claimed and nothing has finished with --
# reused by the quiescence stray-check and by cancel(), which is
# specifically "stop this before it happens", not "annotate an outcome".
NON_TERMINAL_STATUSES = ("pending_score", "queued", "in_progress")

# Recursive CTE shared with crawl/graph.py's subtree-scoped projection
# queries -- same parent_url relationship, same shape, kept in one place
# so the two never drift on how "descendant" is defined.
SUBTREE_CTE = """
WITH RECURSIVE subtree(url) AS (
    SELECT url FROM frontier WHERE {seed_clause}
    UNION ALL
    SELECT f.url FROM frontier f JOIN subtree s ON f.parent_url = s.url
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class FrontierRow:
    url: str
    host: str
    depth: int
    section_label: str | None
    parent_url: str | None
    status: str
    relevance_score: float | None
    retry_count: int


# last_error of a row failed by robots.txt before any request was made
# (crawl_worker -> mark_permanently_failed). Shared with crawl/pipeline.py
# so the budget predicate below can tell it apart from a genuine failure.
ROBOTS_DISALLOWED_ERROR = "robots_disallowed"

# Statuses that consume max_pages. Kept in ONE place on purpose: claim()
# and the quiescence check must agree exactly, or a run could stop claiming
# without ever quiescing (or quiesce while still claimable).
_BUDGET_STATUSES_SQL = "status IN ('done','skipped_extract','mapped')"


class Frontier:
    def __init__(
        self,
        db_path: str,
        max_pages: int | None = None,
        max_depth: int | None = None,
        follow_gate_exempt_depth: int = FOLLOW_GATE_EXEMPT_DEPTH,
        max_retries: int = MAX_RETRIES,
        count_failed_toward_budget: bool = False,
    ):
        """count_failed_toward_budget (config.MAX_PAGES_COUNTS_FAILED, off
        by default): also count terminally `failed` rows toward max_pages --
        genuine fetch/extraction failures only, never a robots.txt disallow
        (no request was made). Without it, a run whose pages keep failing
        never reaches its budget and keeps claiming new pages (ROADMAP.md
        #54). Like done rows, failed rows already in a resumed frontier.db
        count too."""
        self._db_path = db_path
        self._max_pages = max_pages
        self._budget_sql = (
            f"({_BUDGET_STATUSES_SQL} OR (status = 'failed' "
            f"AND COALESCE(last_error, '') != '{ROBOTS_DISALLOWED_ERROR}'))"
            if count_failed_toward_budget else _BUDGET_STATUSES_SQL
        )
        self._max_depth = max_depth
        self._follow_gate_exempt_depth = follow_gate_exempt_depth
        self._max_retries = max_retries

        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._in_flight = 0
        # Graceful stop (headless v1, Brief 3A Step 3): once set, claim() hands
        # out nothing new. In memory only -- unlike `paused`, it belongs to
        # this process's shutdown and must not survive into a resume.
        self._draining = False
        # url -> transient-LLM-error releases this run (release_llm_unavailable).
        # In memory on purpose: the cap bounds one run; a resume starts again.
        self._llm_releases: dict[str, int] = {}
        self._shutdown_triggered = False
        self.quiescent = asyncio.Event()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError(
                f"frontier.py requires SQLite >= {'.'.join(map(str, MIN_SQLITE_VERSION))} "
                f"(UPDATE ... RETURNING) -- this Python's sqlite3 module reports "
                f"{sqlite3.sqlite_version}. Upgrade Python or the system SQLite."
            )
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # _locked_* layer -- assumes self._lock is held. Never acquires it.
    # Never calls a public method.
    # ------------------------------------------------------------------

    async def _locked_is_paused(self) -> bool:
        cur = await self._conn.execute("SELECT paused FROM control WHERE id = 1")
        row = await cur.fetchone()
        return bool(row and row[0])

    async def _locked_check_quiescence(self) -> None:
        if self._in_flight != 0 or self._shutdown_triggered:
            return
        # Explicit, not just a corollary of the queued-count check below:
        # a paused run makes claim() return None while rows are still
        # genuinely 'queued' in the DB, so in_flight can genuinely reach 0
        # (every in-flight page finished naturally, nothing new claimed) --
        # "in_flight==0 and nothing claimable" is exactly quiescence's own
        # trigger condition, and this is the third shape of that same bug
        # (see LESSONS_LEARNED.md #44 and the cancellation work before this).
        # Checked fresh from the DB, not a cached attribute -- pause is set
        # by a different OS process (the server) than the one running this
        # claim loop, so nothing here can assume its own process's view of
        # "paused" is current.
        if await self._locked_is_paused():
            return
        cur = await self._conn.execute("SELECT COUNT(*) FROM frontier WHERE status='queued'")
        queued_n = (await cur.fetchone())[0]
        done_n = None
        if self._max_pages is not None:
            cur = await self._conn.execute(f"SELECT COUNT(*) FROM frontier WHERE {self._budget_sql}")
            done_n = (await cur.fetchone())[0]
        cap_reached = self._max_pages is not None and done_n >= self._max_pages
        if queued_n == 0 or cap_reached:
            cur = await self._conn.execute(
                "SELECT COUNT(*) FROM frontier WHERE status IN "
                "('queued','pending_score','in_progress')"
            )
            stray = (await cur.fetchone())[0]
            # max_pages tripped with queued rows deliberately left live is
            # correct, expected behavior (see CLAUDE.md) -- subtract them
            # out unconditionally (a no-op when queued_n is already 0, the
            # non-cap-reached branch) so only pending_score/in_progress
            # count toward the invariant check below.
            stray -= queued_n
            assert stray == 0, (
                f"quiescence invariant violated: in_flight=0 but {stray} frontier "
                f"rows are still pending_score/in_progress"
            )
            self._shutdown_triggered = True
            self.quiescent.set()

    async def _locked_claim(self) -> FrontierRow | None:
        if self._draining:
            return None  # stopping: rows stay 'queued'
        if await self._locked_is_paused():
            return None  # rows stay 'queued' in the DB -- nothing here changes their status
        now = _now()
        cur = await self._conn.execute(
            f"""
            UPDATE frontier
            SET status = 'in_progress', updated_at = ?
            WHERE url = (
                SELECT url FROM frontier
                WHERE status = 'queued'
                AND (not_before IS NULL OR not_before <= ?)
                ORDER BY created_at LIMIT 1
            )
            AND (
                ? IS NULL OR
                (SELECT COUNT(*) FROM frontier WHERE {self._budget_sql}) < ?
            )
            RETURNING *
            """,
            (now, now, self._max_pages, self._max_pages),
        )
        row = await cur.fetchone()
        await self._conn.commit()
        if row is None:
            return None
        self._in_flight += 1
        return FrontierRow(
            url=row["url"], host=row["host"], depth=row["depth"],
            section_label=row["section_label"], parent_url=row["parent_url"],
            status=row["status"], relevance_score=row["relevance_score"],
            retry_count=row["retry_count"],
        )

    async def _locked_insert_children(
        self, parent_url: str, parent_depth: int, children: list[tuple[str, str | None]]
    ) -> None:
        child_depth = parent_depth + 1
        if self._max_depth is not None and child_depth > self._max_depth:
            return  # discovery cap: never inserted at all

        # A subtree cancelled while this parent was already in_progress
        # (fetch in flight) can't be interrupted -- see cancel()'s
        # docstring -- but its children must not resurrect the subtree.
        # This is the actual enforcement point: the parent's own status
        # flips to 'cancelled' immediately on cancel(), so by the time
        # this fetch completes and reaches here, the check below sees it
        # and refuses to insert (or edge-record) anything downstream.
        cur = await self._conn.execute("SELECT status FROM frontier WHERE url = ?", (parent_url,))
        parent_row = await cur.fetchone()
        if parent_row is not None and parent_row["status"] == "cancelled":
            return

        exempt = parent_depth <= self._follow_gate_exempt_depth
        status = "queued" if exempt else "pending_score"
        now = _now()
        rows = [
            (url, self._host_of(url), child_depth, label, parent_url, status, now, now)
            for url, label in children
        ]
        await self._conn.executemany(
            """
            INSERT OR IGNORE INTO frontier
                (url, host, depth, section_label, parent_url, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        # Every (parent, child) edge, even when the child row already
        # existed (a second parent linking to an already-discovered page)
        # -- INSERT OR IGNORE above silently drops that case, which is
        # exactly the real inbound-link-count information this table
        # exists to keep. See crawl/graph.py.
        edge_rows = [(parent_url, url, now) for url, _label in children]
        await self._conn.executemany(
            "INSERT OR IGNORE INTO discovery_edges (parent_url, child_url, discovered_at) "
            "VALUES (?, ?, ?)",
            edge_rows,
        )
        await self._conn.commit()

    async def _locked_resolve_children(self, parent_url: str, promote: bool) -> None:
        target = "queued" if promote else "skipped_follow"
        await self._conn.execute(
            "UPDATE frontier SET status = ?, updated_at = ? "
            "WHERE parent_url = ? AND status = 'pending_score'",
            (target, _now(), parent_url),
        )
        await self._conn.commit()

    async def _locked_record_score(self, url: str, score: float) -> None:
        await self._conn.execute(
            "UPDATE frontier SET relevance_score = ?, updated_at = ? WHERE url = ?",
            (score, _now(), url),
        )
        await self._conn.commit()

    async def _locked_leave_in_progress(self, url: str) -> None:
        """Shared by every path that leaves in_progress, whether to a
        terminal state or back to queued for retry -- in_flight tracks
        'actively claimed right now', so any exit closes the claim's
        contribution; a retry's next claim() will add its own."""
        self._in_flight -= 1
        await self._locked_check_quiescence()

    async def _locked_mark_terminal(self, url: str, status: str, error: str | None = None) -> None:
        assert status in ("done", "failed", "skipped_extract", "mapped")
        await self._conn.execute(
            "UPDATE frontier SET status = ?, last_error = ?, updated_at = ? WHERE url = ?",
            (status, error, _now(), url),
        )
        if status == "failed":
            # Orphan rule: failed is the only terminal state with no score,
            # so it's the only one that can leave pending_score children
            # waiting for a score that will never come.
            await self._conn.execute(
                "UPDATE frontier SET status = 'skipped_follow', updated_at = ? "
                "WHERE parent_url = ? AND status = 'pending_score'",
                (_now(), url),
            )
        await self._conn.commit()
        await self._locked_leave_in_progress(url)

    async def _locked_retry_or_fail(
        self, url: str, error: str, backoff_seconds: float = 0.0
    ) -> bool:
        """Returns True if the row is now terminally failed, False if it
        was requeued for another attempt. backoff_seconds sets not_before
        instead of sleeping in-place -- releasing the row rather than
        holding a worker slot idle for the delay. A rate-limit caller
        passes a real delay (from Retry-After or an exponential backoff);
        an ordinary fetch/extraction failure passes 0 (immediate retry
        eligibility, still subject to the worker naturally being busy with
        other queued work first)."""
        cur = await self._conn.execute("SELECT retry_count FROM frontier WHERE url = ?", (url,))
        row = await cur.fetchone()
        retry_count = row["retry_count"] + 1
        if retry_count >= self._max_retries:
            await self._conn.execute(
                "UPDATE frontier SET retry_count = ?, updated_at = ? WHERE url = ?",
                (retry_count, _now(), url),
            )
            await self._locked_mark_terminal(url, "failed", error)
            return True
        not_before = None
        if backoff_seconds > 0:
            not_before = (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)).isoformat()
        await self._conn.execute(
            "UPDATE frontier SET status = 'queued', retry_count = ?, last_error = ?, "
            "not_before = ?, updated_at = ? WHERE url = ?",
            (retry_count, error, not_before, _now(), url),
        )
        await self._conn.commit()
        await self._locked_leave_in_progress(url)
        return False

    @staticmethod
    def _host_of(url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).netloc

    # ------------------------------------------------------------------
    # public layer -- acquires the lock, delegates to _locked_*
    # ------------------------------------------------------------------

    async def seed(self, urls: list[tuple[str, str | None]]) -> None:
        """main.py always calls this right after recover_crashed(), even on
        a brand-new frontier.db -- and recover_crashed() unconditionally
        quiesces an empty frontier (see its docstring / LESSONS_LEARNED
        #44), since "empty because nothing seeded yet" and "empty because
        a prior process finished" are indistinguishable at that point.
        seed() is what tells them apart: if it inserts rows that didn't
        already exist, whatever quiescence recover_crashed() concluded is
        now stale and must be un-latched -- otherwise every worker's
        `await frontier.quiescent.wait()` returns immediately and the run
        exits having fetched nothing (found running a real first-time
        crawl; no prior test seeded *after* recover_crashed() on a fresh
        db in the same process, only checked recover_crashed()'s own
        result in isolation).

        Detected via total_changes before/after, not INSERT OR IGNORE's
        executemany rowcount (unreliable across sqlite3/aiosqlite
        versions) -- total_changes only increments for rows actually
        inserted, never for ignored duplicates, so a re-seed of a
        genuinely completed run's original URLs (all already terminal)
        correctly sees inserted=0 and leaves quiescent alone, rather than
        reintroducing the #44 hang by un-latching a run with no new work
        to ever re-trigger the check."""
        now = _now()
        rows = [
            (url, self._host_of(url), 0, label, None, "queued", now, now)
            for url, label in urls
        ]
        async with self._lock:
            before = self._conn.total_changes
            await self._conn.executemany(
                """
                INSERT OR IGNORE INTO frontier
                    (url, host, depth, section_label, parent_url, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await self._conn.commit()
            inserted = self._conn.total_changes - before
            if inserted > 0 and self._shutdown_triggered:
                self._shutdown_triggered = False
                self.quiescent.clear()

    async def claim(self) -> FrontierRow | None:
        async with self._lock:
            return await self._locked_claim()

    async def insert_children(
        self, parent_url: str, parent_depth: int, children: list[tuple[str, str | None]]
    ) -> None:
        if not children:
            return
        async with self._lock:
            await self._locked_insert_children(parent_url, parent_depth, children)

    async def resolve_children(self, parent_url: str, promote: bool) -> None:
        async with self._lock:
            await self._locked_resolve_children(parent_url, promote)

    async def score_and_resolve_children(self, url: str, score: float, promote: bool) -> None:
        """One atomic step: record this page's own relevance score, and
        resolve its pending_score children (promote to queued or drop to
        skipped_follow) based on the same score vs. follow_threshold --
        the caller (extract worker) computes `promote`, this just commits
        both effects together so a crash can't split them."""
        async with self._lock:
            await self._locked_record_score(url, score)
            await self._locked_resolve_children(url, promote)

    async def mark_fetch_failed(self, url: str, error: str, backoff_seconds: float = 0.0) -> bool:
        async with self._lock:
            return await self._locked_retry_or_fail(url, error, backoff_seconds)

    async def mark_extract_outcome(
        self, url: str, status: str, error: str | None = None, backoff_seconds: float = 0.0
    ) -> bool:
        """status: 'skipped_extract' (direct terminal, not a failure) or
        'failed' (retried like a fetch failure -- e.g. a 429 from the LLM
        provider, passing backoff_seconds so the row waits via not_before
        instead of a worker sleeping in place)."""
        assert status in ("skipped_extract", "failed")
        async with self._lock:
            if status == "skipped_extract":
                await self._locked_mark_terminal(url, "skipped_extract")
                return True
            return await self._locked_retry_or_fail(url, error or "extraction failed", backoff_seconds)

    async def mark_permanently_failed(self, url: str, error: str) -> None:
        """Direct terminal failure, no retry accounting -- for failures a
        retry can't possibly fix (robots.txt disallow, a URL that's
        out-of-scope by the time it's claimed, etc), as opposed to
        mark_fetch_failed's retry-then-eventually-fail path."""
        async with self._lock:
            await self._locked_mark_terminal(url, "failed", error)

    async def mark_written(self, url: str) -> None:
        """Writer-owned: only called after the JSONL/Chroma write actually
        succeeds, so 'done' means genuinely persisted, not just attempted."""
        async with self._lock:
            await self._locked_mark_terminal(url, "done")

    async def mark_write_failed(self, url: str, error: str, backoff_seconds: float = 0.0) -> bool:
        async with self._lock:
            return await self._locked_retry_or_fail(url, error, backoff_seconds)

    async def mark_mapped(self, url: str) -> None:
        """map_worker-owned, parallel to mark_written: a page was fetched
        and its links discovered, but no relevance score and no extraction
        was ever attempted -- deliberately a distinct terminal status from
        skipped_extract (which means the relevance gate *did* run and
        rejected the page). Conflating the two would make a map run's
        counts_by_status() look like a mass relevance-gate rejection."""
        async with self._lock:
            await self._locked_mark_terminal(url, "mapped")

    async def set_paused(self, paused: bool) -> None:
        """Stops new claims without touching in-flight or queued rows --
        see the 'control' table's schema comment for why this is a DB
        flag, not a Python attribute: the caller pausing a run (the
        server) is a different OS process than the one running claim()
        (the crawl subprocess), and only the DB is shared between them.
        In-flight fetches/extractions finish naturally either way (no
        worker-loop change needed -- they already poll-and-sleep whenever
        claim() returns None, same as "nothing queued right now").
        Unpausing re-checks quiescence immediately, in case the answer
        was simply blocked on this flag the whole time (see
        _locked_check_quiescence)."""
        async with self._lock:
            await self._conn.execute("UPDATE control SET paused = ? WHERE id = 1", (int(paused),))
            await self._conn.commit()
            if not paused:
                await self._locked_check_quiescence()

    async def is_paused(self) -> bool:
        async with self._lock:
            return await self._locked_is_paused()

    @staticmethod
    def _escape_like(s: str) -> str:
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    async def cancel(self, *, url: str | None = None, prefix: str | None = None) -> list[str]:
        """Cancel a node (or every node whose URL starts with prefix) and
        every descendant reachable through parent_url, in one transaction.
        Exactly one of url/prefix must be given.

        Only touches rows currently in a NON_TERMINAL_STATUSES status --
        rows already done/failed/skipped_extract/skipped_follow/mapped are
        left alone (already-terminal work is not undone), and re-cancelling
        an already-cancelled row is a no-op.

        In-flight handling: a row that is 'in_progress' right now (a fetch
        or extraction actually in flight) is cancelled here too -- but
        this pipeline has no mechanism to abort an in-flight crawl4ai fetch
        or LLM call mid-request (and building one is out of scope: same
        conservative posture as BrowserDriverError's fail-fast design,
        nothing here has been proven safe to interrupt mid-call). So it
        finishes naturally and overwrites this 'cancelled' status with its
        real outcome (done/failed/skipped_extract) when the owning worker
        calls its normal mark_* method -- cancellation does not undo work
        already in flight, it only stops *new* work from starting.
        What it does stop: _locked_insert_children checks the parent's
        current status before inserting anything, so a node that finishes
        fetching after being cancelled mid-flight does not propagate any
        new children into the frontier -- the subtree can't regrow through
        the one node cancel() couldn't reach in time.
        """
        if (url is None) == (prefix is None):
            raise ValueError("cancel() requires exactly one of url or prefix")
        if url is not None:
            seed_clause, seed_params = "url = ?", (url,)
        else:
            seed_clause, seed_params = "url LIKE ? ESCAPE '\\'", (self._escape_like(prefix) + "%",)

        non_terminal_list = ", ".join(f"'{s}'" for s in NON_TERMINAL_STATUSES)
        query = SUBTREE_CTE.format(seed_clause=seed_clause) + (
            "UPDATE frontier SET status = 'cancelled', updated_at = ? "
            f"WHERE url IN (SELECT url FROM subtree) AND status IN ({non_terminal_list}) "
            "RETURNING url"
        )
        async with self._lock:
            cur = await self._conn.execute(query, (*seed_params, _now()))
            cancelled = [row["url"] for row in await cur.fetchall()]
            await self._conn.commit()
            await self._locked_check_quiescence()
            return cancelled

    async def requeue_for_extraction(self, *, url: str | None = None, prefix: str | None = None) -> list[str]:
        """The 'select' step of map-then-select-then-extract: promote a
        previously-mapped node (or every mapped node under a URL prefix)
        and its mapped descendants back to 'queued', so a resumed run
        re-fetches and actually extracts them. Content from the map pass
        was never persisted (it only ever lived in the in-memory
        content_queue -- see CLAUDE.md), so 'resume' here means re-fetch,
        same as crash recovery's requeue path.

        Traversal stops at a 'cancelled' node instead of walking through
        it -- a prior cancel() is a deliberate exclusion decision, and
        selecting an ancestor for extraction should not silently resurrect
        a subtree that was explicitly cancelled underneath it.
        Only rows currently 'mapped' are touched; already-queued,
        -cancelled, or -failed rows are left alone.
        """
        if (url is None) == (prefix is None):
            raise ValueError("requeue_for_extraction() requires exactly one of url or prefix")
        if url is not None:
            seed_clause, seed_params = "url = ? AND status != 'cancelled'", (url,)
        else:
            seed_clause, seed_params = (
                "url LIKE ? ESCAPE '\\' AND status != 'cancelled'",
                (self._escape_like(prefix) + "%",),
            )

        query = (
            f"""
            WITH RECURSIVE subtree(url) AS (
                SELECT url FROM frontier WHERE {seed_clause}
                UNION ALL
                SELECT f.url FROM frontier f JOIN subtree s ON f.parent_url = s.url
                WHERE f.status != 'cancelled'
            )
            UPDATE frontier SET status = 'queued', updated_at = ?
            WHERE url IN (SELECT url FROM subtree) AND status = 'mapped'
            RETURNING url
            """
        )
        async with self._lock:
            cur = await self._conn.execute(query, (*seed_params, _now()))
            requeued = [row["url"] for row in await cur.fetchall()]
            await self._conn.commit()
            if requeued and self._shutdown_triggered:
                self._shutdown_triggered = False
                self.quiescent.clear()
            return requeued

    async def put_content(self, content_queue: asyncio.Queue, item) -> None:
        async with self._lock:
            self._in_flight += 1
        await content_queue.put(item)  # lock released before the blocking put

    async def content_done(self, content_queue: asyncio.Queue) -> None:
        content_queue.task_done()  # asyncio-internal bookkeeping, no lock needed
        async with self._lock:
            self._in_flight -= 1
            await self._locked_check_quiescence()

    async def put_results(self, results_queue: asyncio.Queue, item) -> None:
        async with self._lock:
            self._in_flight += 1
        await results_queue.put(item)

    async def results_done(self, results_queue: asyncio.Queue) -> None:
        results_queue.task_done()
        async with self._lock:
            self._in_flight -= 1
            await self._locked_check_quiescence()

    @property
    def in_flight(self) -> int:
        """Claimed rows not yet finished (fetching, queued for extraction,
        extracting, or waiting to be written)."""
        return self._in_flight

    def begin_drain(self) -> None:
        """Stop handing out new rows; work already claimed carries on."""
        self._draining = True

    # -- headless contract v2: request forms and robots counts (Brief 3B Step 1) --
    # A SIDE table, created only by v2 runs, so legacy/v1 frontier.db files are
    # never altered. Maps a row's identity (normalize_url) to the form it was
    # first discovered in, when that differs (a trailing slash: ROADMAP #53).
    async def enable_fetch_urls(self) -> None:
        async with self._lock:
            await self._conn.execute(
                "CREATE TABLE IF NOT EXISTS fetch_urls (url TEXT PRIMARY KEY, fetch_url TEXT NOT NULL)")
            await self._conn.commit()

    async def record_fetch_urls(self, pairs: list[tuple[str, str]]) -> None:
        """(identity url, request url) pairs; first discoverer wins, like parent_url."""
        differing = [(u, f) for u, f in pairs if f != u]
        if not differing:
            return
        async with self._lock:
            await self._conn.executemany("INSERT OR IGNORE INTO fetch_urls (url, fetch_url) VALUES (?, ?)", differing)
            await self._conn.commit()

    async def fetch_url_for(self, url: str) -> str:
        async with self._lock:
            cur = await self._conn.execute("SELECT fetch_url FROM fetch_urls WHERE url = ?", (url,))
            row = await cur.fetchone()
        return row[0] if row else url

    async def robots_disallowed_by_host(self, robots_error: str) -> dict[str, dict]:
        """host -> {"urls": rows failed by robots.txt, "seeds": depth-0 rows,
        "seeds_disallowed": depth-0 rows failed by robots.txt}."""
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT host, SUM(last_error = ?), SUM(depth = 0), SUM(depth = 0 AND last_error = ?) "
                "FROM frontier GROUP BY host", (robots_error, robots_error))
            rows = await cur.fetchall()
        return {h.lower(): {"urls": int(u or 0), "seeds": int(s or 0), "seeds_disallowed": int(sd or 0)}
                for h, u, s, sd in rows}

    async def release_llm_unavailable(self, url: str, reason: str, backoff_for, max_releases: int) -> bool:
        """A transient provider error (5xx) on this page: put it back in
        'queued' with not_before = backoff_for(n) seconds, where n is this
        page's release count this run, WITHOUT counting a retry -- the page
        did nothing wrong. After max_releases releases the next call marks it
        'failed' with a reason saying so. Returns True if it is now failed."""
        async with self._lock:
            n = self._llm_releases.get(url, 0) + 1
            self._llm_releases[url] = n
            if n > max_releases:
                await self._locked_mark_terminal(
                    url, "failed", f"{reason}; gave up after {max_releases} releases (llm_unavailable_max_releases)")
                return True
            not_before = (datetime.now(UTC) + timedelta(seconds=backoff_for(n))).isoformat()
            await self._conn.execute(
                "UPDATE frontier SET status = 'queued', last_error = ?, not_before = ?, updated_at = ? WHERE url = ?",
                (f"{reason} (release {n}/{max_releases})", not_before, _now(), url),
            )
            await self._conn.commit()
            await self._locked_leave_in_progress(url)
            return False

    async def release_in_progress(self, reason: str) -> int:
        """Put every still-claimed row back to 'queued' for a later resume,
        WITHOUT counting a retry: a requested stop isn't a failure of the page
        (unlike recover_crashed(), which treats an unexplained in_progress row
        as one). Returns how many rows were released."""
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE frontier SET status = 'queued', last_error = ?, updated_at = ? "
                "WHERE status = 'in_progress'", (reason, _now()))
            await self._conn.commit()
            self._in_flight = 0
            return cur.rowcount

    async def recover_crashed(self) -> dict[str, int]:
        """Startup only, before any worker runs -- no in_flight bookkeeping
        needed, since in_flight is this-process-lifetime only and starts at
        0 regardless of what a prior crashed process left behind."""
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT url, retry_count FROM frontier WHERE status = 'in_progress'"
            )
            rows = await cur.fetchall()
            requeued, failed = 0, 0
            for row in rows:
                url, retry_count = row["url"], row["retry_count"] + 1
                if retry_count >= self._max_retries:
                    await self._conn.execute(
                        "UPDATE frontier SET status='failed', retry_count=?, "
                        "last_error='crash_recovery_exhausted_retries', updated_at=? WHERE url=?",
                        (retry_count, _now(), url),
                    )
                    await self._conn.execute(
                        "UPDATE frontier SET status='skipped_follow', updated_at=? "
                        "WHERE parent_url=? AND status='pending_score'",
                        (_now(), url),
                    )
                    failed += 1
                else:
                    await self._conn.execute(
                        "UPDATE frontier SET status='queued', retry_count=?, "
                        "last_error='crash_recovery_requeued', updated_at=? WHERE url=?",
                        (retry_count, _now(), url),
                    )
                    requeued += 1
            await self._conn.commit()
            # Unconditional, not just a cap-blocked claim() branch: the
            # quiescence check is otherwise only reachable from a decrement
            # (claim succeeding, put_content, put_results/content_done) --
            # every one of those requires something to still be in flight.
            # A process that starts in an already-final state (empty
            # frontier, everything already terminal, or max_pages already
            # met with nothing left in_progress to recover) has no such
            # decrement to ever reach it from, so quiescent never gets set
            # and every worker loops claim()->None forever. Same shape as
            # the cascade-termination bug this architecture was built to
            # avoid -- a completion-triggered check in a state where
            # nothing completes. See LESSONS_LEARNED.md #44.
            await self._locked_check_quiescence()
            return {"requeued": requeued, "failed": failed}

    async def counts_by_status(self) -> dict[str, int]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT status, COUNT(*) FROM frontier GROUP BY status"
            )
            return dict(await cur.fetchall())

    async def get_all_urls(self) -> set[str]:
        """Read-only, for tests/debugging -- every url ever inserted."""
        async with self._lock:
            cur = await self._conn.execute("SELECT url FROM frontier")
            return {row["url"] for row in await cur.fetchall()}

    async def all_scores(self) -> list[tuple[str, float, str]]:
        """Read-only, for --score-report -- every page that got a
        relevance score recorded, regardless of what happened to it
        afterward. (url, relevance_score, status)."""
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT url, relevance_score, status FROM frontier "
                "WHERE relevance_score IS NOT NULL ORDER BY relevance_score DESC"
            )
            return [(row["url"], row["relevance_score"], row["status"]) for row in await cur.fetchall()]

    async def in_progress_urls(self, limit: int = 20) -> list[str]:
        """Read-only, for the progress display -- never mutates state, so
        it can't perturb the state machine no matter how often it's polled."""
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT url FROM frontier WHERE status = 'in_progress' "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
            return [row["url"] for row in await cur.fetchall()]
