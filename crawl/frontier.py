"""SQLite-backed crawl frontier: durable URL state, the visited-set, and the
in_flight/quiescence bookkeeping that decides when a run is actually done.

State machine (see CLAUDE.md for the full table): pending_score -> queued ->
in_progress -> done / failed / skipped_follow / skipped_extract. done and
skipped_extract both imply a relevance score was computed; failed is the only
terminal state that doesn't, which is why orphan resolution keys off it alone
(see _locked_mark_terminal).

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
from datetime import datetime, timedelta, timezone

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
                        'done', 'failed', 'skipped_follow', 'skipped_extract'
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


class Frontier:
    def __init__(
        self,
        db_path: str,
        max_pages: int | None = None,
        max_depth: int | None = None,
        follow_gate_exempt_depth: int = FOLLOW_GATE_EXEMPT_DEPTH,
        max_retries: int = MAX_RETRIES,
    ):
        self._db_path = db_path
        self._max_pages = max_pages
        self._max_depth = max_depth
        self._follow_gate_exempt_depth = follow_gate_exempt_depth
        self._max_retries = max_retries

        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._in_flight = 0
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

    async def _locked_check_quiescence(self) -> None:
        if self._in_flight != 0 or self._shutdown_triggered:
            return
        cur = await self._conn.execute("SELECT COUNT(*) FROM frontier WHERE status='queued'")
        queued_n = (await cur.fetchone())[0]
        done_n = None
        if self._max_pages is not None:
            cur = await self._conn.execute(
                "SELECT COUNT(*) FROM frontier WHERE status IN ('done','skipped_extract')"
            )
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
        now = _now()
        cur = await self._conn.execute(
            """
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
                (SELECT COUNT(*) FROM frontier WHERE status IN ('done','skipped_extract')) < ?
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
        assert status in ("done", "failed", "skipped_extract")
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
