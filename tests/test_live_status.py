"""crawl/live_status.py: read_live_status() against a plain sqlite3-written
fixture (not the async Frontier) -- proves this reads a real frontier.db
file from a separate connection/process, the actual point of this module
(a future UI backend polling a crawl this process isn't running)."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crawl.frontier import SCHEMA
from crawl.live_status import read_live_status

HOST = "https://x.test"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestReadLiveStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "frontier.db")
        self.now = datetime.now(timezone.utc)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        rows = [
            (f"{HOST}/a", "x.test", 0, None, None, "done", _iso(self.now - timedelta(seconds=30))),
            (f"{HOST}/b", "x.test", 1, None, f"{HOST}/a", "in_progress", _iso(self.now - timedelta(seconds=12))),
            (f"{HOST}/c", "x.test", 1, None, f"{HOST}/a", "in_progress", _iso(self.now - timedelta(seconds=3))),
            (f"{HOST}/d", "x.test", 2, None, f"{HOST}/b", "queued", _iso(self.now - timedelta(seconds=1))),
            (f"{HOST}/e", "x.test", 1, None, f"{HOST}/a", "failed", _iso(self.now - timedelta(seconds=5))),
        ]
        conn.executemany(
            "INSERT INTO frontier (url, host, depth, section_label, parent_url, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(*r[:6], r[6], r[6]) for r in rows],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_counts_by_status(self):
        status = read_live_status(self.db_path)
        self.assertEqual(status["counts_by_status"], {
            "done": 1, "in_progress": 2, "queued": 1, "failed": 1,
        })

    def test_in_flight_includes_parent_and_running_seconds_ordered_oldest_first(self):
        status = read_live_status(self.db_path)
        in_flight = status["in_flight"]
        self.assertEqual([n["url"] for n in in_flight], [f"{HOST}/b", f"{HOST}/c"])
        self.assertEqual(in_flight[0]["parent_url"], f"{HOST}/a")
        # b claimed ~12s ago, c claimed ~3s ago
        self.assertGreater(in_flight[0]["running_seconds"], in_flight[1]["running_seconds"])
        self.assertAlmostEqual(in_flight[0]["running_seconds"], 12, delta=2)
        self.assertAlmostEqual(in_flight[1]["running_seconds"], 3, delta=2)
        self.assertEqual(in_flight[0]["id"], in_flight[0]["url"])  # stable id == url

    def test_recently_completed_excludes_live_statuses(self):
        status = read_live_status(self.db_path)
        recent_statuses = {r["status"] for r in status["recently_completed"]}
        self.assertNotIn("in_progress", recent_statuses)
        self.assertNotIn("queued", recent_statuses)
        self.assertIn("done", recent_statuses)
        self.assertIn("failed", recent_statuses)

    def test_last_change_at_is_the_max_updated_at(self):
        status = read_live_status(self.db_path)
        self.assertEqual(status["last_change_at"], _iso(self.now - timedelta(seconds=1)))  # d

    def test_recent_limit_and_in_flight_limit_are_respected(self):
        status = read_live_status(self.db_path, recent_limit=1, in_flight_limit=1)
        self.assertEqual(len(status["recently_completed"]), 1)
        self.assertEqual(len(status["in_flight"]), 1)

    def test_readable_while_a_second_connection_holds_the_db_open(self):
        """The actual point: this must work concurrently with a running
        crawl's own Frontier connection, not just when nothing else has
        the file open."""
        writer_conn = sqlite3.connect(self.db_path)
        writer_conn.execute("PRAGMA journal_mode=WAL")
        writer_conn.execute(
            "UPDATE frontier SET status='in_progress', updated_at=? WHERE url=?",
            (_iso(self.now), f"{HOST}/d"),
        )
        writer_conn.commit()
        try:
            status = read_live_status(self.db_path)
            self.assertEqual(status["counts_by_status"].get("in_progress"), 3)
        finally:
            writer_conn.close()


if __name__ == "__main__":
    unittest.main()
