"""Read current crawl state from another process without touching the
running pipeline -- same discipline as progress_display.py (only ever
SELECTs), but as a plain sync function over its own short-lived sqlite3
connection rather than the async Frontier/its lock, so it works from a
process that isn't the one running the crawl (a future UI backend, most
likely). WAL mode (set once, at Frontier.open() time, and it sticks with
the DB file from then on) is what makes this a safe concurrent read while
a crawl is actively writing.

Poll-friendly by design: call read_live_status() again for a fresh
snapshot. No push mechanism here -- not needed yet (see CLAUDE.md's
step-1 feature list).
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

_RECENT_STATUSES = ("done", "failed", "skipped_extract", "skipped_follow", "mapped", "cancelled")


def _age_seconds(iso_ts: str, now: datetime) -> float:
    ts = datetime.fromisoformat(iso_ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max((now - ts).total_seconds(), 0.0)


def read_live_status(db_path: str, recent_limit: int = 20, in_flight_limit: int = 50) -> dict:
    """counts_by_status, in-flight rows (with which parent they came from
    and how long they've been running -- edge-level progress for a UI to
    animate), recently-completed rows, and the timestamp of the last
    change to any row. `running_seconds` on an in-flight row is
    `now - updated_at`, valid because nothing touches an in_progress row's
    updated_at between claim() setting it and whichever mark_* call ends
    that claim -- claim() is the only writer in between, so updated_at
    *is* the claim timestamp for as long as status stays 'in_progress'.
    """
    started = time.perf_counter()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM frontier GROUP BY status").fetchall())

        now = datetime.now(timezone.utc)
        in_flight_rows = conn.execute(
            "SELECT url, parent_url, host, depth, updated_at FROM frontier "
            "WHERE status = 'in_progress' ORDER BY updated_at ASC LIMIT ?",
            (in_flight_limit,),
        ).fetchall()
        in_flight = [
            {
                "id": r["url"], "url": r["url"], "parent_url": r["parent_url"],
                "host": r["host"], "depth": r["depth"],
                "running_seconds": round(_age_seconds(r["updated_at"], now), 1),
            }
            for r in in_flight_rows
        ]

        recent_rows = conn.execute(
            f"SELECT url, status, updated_at FROM frontier "
            f"WHERE status IN ({','.join('?' * len(_RECENT_STATUSES))}) "
            f"ORDER BY updated_at DESC LIMIT ?",
            (*_RECENT_STATUSES, recent_limit),
        ).fetchall()
        recently_completed = [
            {"id": r["url"], "url": r["url"], "status": r["status"], "updated_at": r["updated_at"]}
            for r in recent_rows
        ]

        last_change_row = conn.execute("SELECT MAX(updated_at) AS m FROM frontier").fetchone()
        last_change_at = last_change_row["m"] if last_change_row else None

        return {
            "counts_by_status": counts,
            "in_flight": in_flight,
            "recently_completed": recently_completed,
            "last_change_at": last_change_at,
            "queried_at": now.isoformat(),
            "query_seconds": round(time.perf_counter() - started, 4),
        }
    finally:
        conn.close()
