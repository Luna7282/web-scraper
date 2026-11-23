"""Brief 3A Step 3: graceful stop (item 8) and atomic page writes (item 9).

Unit tests for the pieces; then real signals sent to tests/fakes/graceful_run.py
(the real v1 entry, headless_crawl, frontier and Writer with a fake crawler,
fetch and extractor -- no LLM, no network), and a kill-mid-append test.
"""

import asyncio
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import headless
from crawl import graceful
from crawl.frontier import Frontier
from storage import writer as writer_module
from storage.writer import Writer

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------
class TestGracefulStopUnit(unittest.TestCase):
    def test_first_signal_sets_the_event_second_calls_on_second(self):
        seconds = []

        async def scenario():
            stop = graceful.GracefulStop(on_second=seconds.append)
            stop.handle("SIGTERM")
            self.assertTrue(stop.event.is_set())
            self.assertEqual((stop.signal_name, stop.count), ("SIGTERM", 1))
            stop.handle("SIGINT")
            self.assertEqual((stop.signal_name, stop.count), ("SIGTERM", 2))  # the first one decides

        asyncio.run(scenario())
        self.assertEqual(seconds, ["SIGINT"])

    def test_drain_waits_for_in_flight_work_or_the_grace_period(self):
        class FakeFrontier:
            def __init__(self, in_flight):
                self.in_flight, self.drained = in_flight, False

            def begin_drain(self):
                self.drained = True

        async def finishing():
            f = FakeFrontier(3)

            async def finish():
                for _ in range(3):
                    await asyncio.sleep(0.03)
                    f.in_flight -= 1
            asyncio.ensure_future(finish())
            return f, await graceful.drain(f, grace_seconds=5)

        f, info = asyncio.run(finishing())
        self.assertTrue(f.drained)
        self.assertTrue(info["drained"])
        self.assertLess(info["waited_seconds"], 1)
        stuck = FakeFrontier(1)
        info = asyncio.run(graceful.drain(stuck, grace_seconds=0.2))
        self.assertFalse(info["drained"])
        self.assertGreaterEqual(info["waited_seconds"], 0.18)


class TestFrontierDrainAndRelease(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = Frontier(":memory:", max_retries=3)
        await self.f.open()
        await self.f.recover_crashed()
        await self.f.seed([(f"https://x.test/p{i}", None) for i in range(4)])

    async def asyncTearDown(self):
        await self.f.close()

    async def test_draining_stops_new_claims(self):
        self.assertIsNotNone(await self.f.claim())
        self.f.begin_drain()
        self.assertIsNone(await self.f.claim())
        self.assertEqual((await self.f.counts_by_status()).get("queued"), 3)  # still queued, untouched

    async def test_release_puts_claimed_rows_back_without_counting_a_retry(self):
        await self.f.claim()
        await self.f.claim()
        self.assertEqual(self.f.in_flight, 2)
        self.assertEqual(await self.f.release_in_progress("released_on_stop"), 2)
        self.assertEqual(self.f.in_flight, 0)
        self.assertEqual(await self.f.counts_by_status(), {"queued": 4})
        cur = await self.f._conn.execute("SELECT retry_count, last_error FROM frontier WHERE last_error IS NOT NULL")
        rows = await cur.fetchall()
        self.assertEqual([tuple(r) for r in rows], [(0, "released_on_stop")] * 2)


class TestAtomicPageWrites(unittest.IsolatedAsyncioTestCase):
    async def test_a_page_is_one_write_then_one_fsync_with_the_same_bytes(self):
        pairs = [{"question": f"Q{i} — ünïcode", "answer": f"A{i}"} for i in range(5)]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "canonical.jsonl")
            w = Writer(path)
            real_write = os.write
            calls = {"write": 0, "fsync": 0}

            def counting_write(fd, data):
                calls["write"] += 1
                return real_write(fd, data)

            with patch.object(writer_module.os, "write", counting_write), \
                 patch.object(writer_module.os, "fsync", lambda fd: calls.__setitem__("fsync", calls["fsync"] + 1)):
                await w.write("https://x.test/a", pairs)
            expected = "".join(json.dumps({**p, "source_url": "https://x.test/a"}) + "\n" for p in pairs)
            self.assertEqual(Path(path).read_bytes(), expected.encode("utf-8"))  # same bytes as before
            self.assertEqual(calls, {"write": 1, "fsync": 1})


class TestKillMidAppend(unittest.TestCase):
    """SIGKILL a process appending whole pages in a loop; every kill must leave
    only complete pages (40 rows each) and no cut line."""

    def test_sigkill_never_leaves_a_partial_page_or_a_cut_line(self):
        code = (
            "import json, sys\n"
            "from storage.writer import append_page_atomically\n"
            "row = {'question': 'Q' * 60, 'answer': 'A' * 250, 'source_chunk': 'c' * 200}\n"
            "data = ''.join(json.dumps({**row, 'i': i}) + '\\n' for i in range(40)).encode()\n"
            "while True:\n"
            "    append_page_atomically(sys.argv[1], data)\n"
        )
        rng = random.Random(3)
        with tempfile.TemporaryDirectory() as d:
            for k in range(12):
                path = os.path.join(d, f"c{k}.jsonl")
                p = subprocess.Popen([sys.executable, "-c", code, path], cwd=REPO)
                time.sleep(rng.uniform(0.3, 0.6))  # past interpreter start-up, mid-loop
                p.kill()
                p.wait()
                raw = Path(path).read_bytes() if os.path.exists(path) else b""
                self.assertTrue(raw == b"" or raw.endswith(b"\n"), f"kill {k}: cut line")
                self.assertEqual(raw.count(b"\n") % 40, 0, f"kill {k}: partial page")
                for line in raw.split(b"\n")[:-1]:
                    json.loads(line)


class TestRepairOnResume(unittest.TestCase):
    def test_a_cut_tail_is_moved_aside_and_the_file_ends_on_a_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "canonical.jsonl"
            path.write_bytes(b'{"a": 1}\n{"b": 2}\n{"c": 3')
            info = headless.repair_canonical_tail(d, "run-b")
            self.assertEqual(info, {"bytes_moved": 7, "moved_to": "canonical.tail.run-b.bin"})
            self.assertEqual(path.read_bytes(), b'{"a": 1}\n{"b": 2}\n')
            self.assertEqual((Path(d) / "canonical.tail.run-b.bin").read_bytes(), b'{"c": 3')
            self.assertIsNone(headless.repair_canonical_tail(d, "run-c"))  # clean now

    def test_a_missing_or_clean_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(headless.repair_canonical_tail(d, "r"))
            (Path(d) / "canonical.jsonl").write_bytes(b'{"a": 1}\n')
            self.assertIsNone(headless.repair_canonical_tail(d, "r"))


# ---------------------------------------------------------------------------
# real signals against the fake run
# ---------------------------------------------------------------------------
@unittest.skipUnless(os.name == "posix", "POSIX signals")
class TestSignalsOnARun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, mode="v1", delay=0.2, grace=10, resume=False):
        args = [sys.executable, "-m", "tests.fakes.graceful_run", str(self.run_dir), mode, str(delay), str(grace)]
        if resume:
            args.append("resume")
        return subprocess.Popen(args, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "ANONYMIZED_TELEMETRY": "False"})

    def wait_rows(self, n, timeout=60):
        path = self.run_dir / "canonical.jsonl"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.read_bytes().count(b"\n") >= n:
                return
            time.sleep(0.05)
        self.fail(f"fewer than {n} rows written in {timeout}s")

    def frontier_counts(self):
        con = sqlite3.connect(self.run_dir / "frontier.db")
        try:
            return dict(con.execute("SELECT status, COUNT(*) FROM frontier GROUP BY status").fetchall())
        finally:
            con.close()

    def result(self):
        return json.loads((self.run_dir / "result.json").read_text())

    def assert_clean_file(self):
        raw = (self.run_dir / "canonical.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        urls = [json.loads(line)["source_url"] for line in raw.splitlines()]
        self.assertEqual(len(urls), len(set(urls)))  # one row per page, no duplicates

    def test_sigterm_drains_closes_the_crawler_and_exits_143(self):
        p = self.start(delay=0.2, grace=10)
        self.wait_rows(3)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=60)
        r = self.result()
        self.assertEqual((p.returncode, r["outcome"], r["exit_code"], r["signal"]), (143, "stopped_by_signal", 143, "SIGTERM"))
        self.assertTrue(r["stop"]["drained"])
        self.assertEqual(r["stop"]["released_rows"], 0)
        self.assertTrue((self.run_dir / "crawler_closed").exists())
        counts = self.frontier_counts()
        self.assertNotIn("in_progress", counts)
        self.assertGreater(counts.get("queued", 0), 0)  # the rest is left for a resume
        self.assert_clean_file()

    def test_sigint_exits_130_as_interrupted(self):
        p = self.start(delay=0.2)
        self.wait_rows(2)
        p.send_signal(signal.SIGINT)
        p.communicate(timeout=60)
        self.assertEqual((p.returncode, self.result()["outcome"]), (130, "interrupted"))
        self.assertNotIn("in_progress", self.frontier_counts())

    def test_grace_exceeded_releases_rows_without_a_retry(self):
        p = self.start(delay=4.0, grace=0)  # extraction far slower than the grace period
        self.wait_rows(0)
        time.sleep(4)  # pages claimed and stuck in extraction
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=60)
        r = self.result()
        self.assertEqual(p.returncode, 143)
        self.assertFalse(r["stop"]["drained"])
        self.assertGreater(r["stop"]["released_rows"], 0)
        con = sqlite3.connect(self.run_dir / "frontier.db")
        released = con.execute("SELECT retry_count FROM frontier WHERE last_error = 'released_on_stop'").fetchall()
        con.close()
        self.assertEqual({row[0] for row in released}, {0})
        self.assertNotIn("in_progress", self.frontier_counts())

    def test_a_second_signal_exits_immediately(self):
        p = self.start(delay=0.5, grace=30)
        self.wait_rows(1)
        p.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        started = time.monotonic()
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 5)
        r = self.result()
        self.assertEqual((p.returncode, r["outcome"], r["signal"]), (143, "stopped_immediately", "SIGTERM"))
        self.assertFalse((self.run_dir / "crawler_closed").exists())  # no cleanup ran

    def test_resume_after_a_graceful_stop_finishes_every_page_once(self):
        p = self.start(delay=0.1, grace=10)
        self.wait_rows(5)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=60)
        first_id = self.result()["run_id"]
        p = self.start(delay=0.01, grace=10, resume=True)
        p.communicate(timeout=120)
        r = self.result()
        self.assertEqual((p.returncode, r["outcome"], r["resumed_from_run_id"]), (0, "ok", first_id))
        self.assertEqual(self.frontier_counts(), {"done": 40})
        self.assert_clean_file()
        self.assertEqual((self.run_dir / "canonical.jsonl").read_bytes().count(b"\n"), 40)

    def test_legacy_runs_install_no_handlers(self):
        """No contract_version: SIGTERM kills the process exactly as today."""
        p = self.start(mode="legacy", delay=0.2)
        self.wait_rows(2)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, -signal.SIGTERM)
        self.assertFalse((self.run_dir / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
