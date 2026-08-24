"""server/app.py routes via Flask's test client -- no real browser, no
real server binding, matching the brief's "every endpoint testable
without a browser." Subprocess-touching routes (discover/start/cancel/
select/pause/query/dataset-report/export) are exercised against a fake
main.py stand-in (a tiny script accepting the same flags) so these tests
stay fast and offline; the real main.py wiring is proven by the live
headless demo, not by these.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from crawl.frontier import SCHEMA
from server.app import create_app
from server import app as app_module
from server.jobs import JobManager


FAKE_MAIN = None  # set in setUpModule to a tmp script path


def setUpModule():
    global FAKE_MAIN
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(FAKE_MAIN_SRC)
    FAKE_MAIN = path


def tearDownModule():
    if FAKE_MAIN and os.path.exists(FAKE_MAIN):
        os.remove(FAKE_MAIN)


FAKE_MAIN_SRC = '''
import argparse, json, sys, time

p = argparse.ArgumentParser()
p.add_argument("--discover-json")
p.add_argument("--headless-config")
p.add_argument("--pause")
p.add_argument("--unpause")
p.add_argument("--cancel")
p.add_argument("--cancel-prefix")
p.add_argument("--cancel-db")
p.add_argument("--select")
p.add_argument("--select-prefix")
p.add_argument("--select-db")
p.add_argument("--dataset-report")
p.add_argument("--dataset-report-frontier-db")
p.add_argument("--query")
p.add_argument("--query-chroma-dir")
args = p.parse_args()

if args.discover_json:
    print(json.dumps({
        "root_url": args.discover_json,
        "branches": [
            {"category": "Path: /docs/*", "urls": ["https://x.test/docs/a", "https://x.test/docs/b"],
             "url_count": 2, "host": "x.test", "prefix": "/docs/", "degenerate": False, "note": "n"},
        ],
    }))
elif args.headless_config:
    with open(args.headless_config, encoding="utf-8") as f:
        cfg = json.load(f)
    time.sleep(0.3)
    print(json.dumps({"mode": cfg["mode"], "counts_by_status": {"mapped": 2}, "fatal_errors": []}))
elif args.pause is not None:
    print("paused")
elif args.unpause is not None:
    print("unpaused")
elif args.cancel or args.cancel_prefix:
    print("cancelled 1 row")
elif args.select or args.select_prefix:
    print("selected 1 row")
elif args.dataset_report:
    print("dataset report text")
elif args.query:
    print("query results text")
'''


class ServerAppTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patch = mock.patch("server.paths.DATA_DIR", self._tmp.name)
        self._data_dir_patch.start()
        self._app_data_dir_patch = mock.patch("server.app.DATA_DIR", self._tmp.name)
        self._app_data_dir_patch.start()
        self._runs_data_dir_patch = mock.patch("server.runs.DATA_DIR", self._tmp.name)
        self._runs_data_dir_patch.start()
        self._main_py_patch = mock.patch("server.app.MAIN_PY", FAKE_MAIN)
        self._main_py_patch.start()

        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        self._data_dir_patch.stop()
        self._app_data_dir_patch.stop()
        self._runs_data_dir_patch.stop()
        self._main_py_patch.stop()
        self._tmp.cleanup()

    def _make_run(self, run_id: str, statuses: dict[str, int]) -> str:
        run_dir = os.path.join(self._tmp.name, run_id)
        os.makedirs(run_dir, exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(os.path.join(run_dir, "frontier.db"))
        conn.executescript(SCHEMA)
        now = "2026-01-01T00:00:00+00:00"
        i = 0
        for status, n in statuses.items():
            for _ in range(n):
                i += 1
                conn.execute(
                    "INSERT INTO frontier (url, host, depth, parent_url, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (f"https://x.test/{i}", "x.test", 0, None, status, now, now),
                )
        conn.commit()
        conn.close()
        return run_dir

    # -- health / run listing --------------------------------------------

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True})

    def test_list_runs_empty(self):
        r = self.client.get("/runs")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"runs": []})

    def test_list_runs_includes_a_real_run(self):
        self._make_run("run-a", {"mapped": 3, "queued": 1})
        r = self.client.get("/runs")
        runs = r.get_json()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], "run-a")
        self.assertEqual(runs[0]["pages_fetched"], 3)

    def test_run_detail_404_for_unknown_run(self):
        r = self.client.get("/runs/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_run_id_path_traversal_rejected(self):
        r = self.client.get("/runs/..%2F..%2Fetc")
        self.assertIn(r.status_code, (400, 404))
        self.assertIn("error", r.get_json())

    # -- discover / start / resume / stop / pause ------------------------

    def test_discover(self):
        r = self.client.post("/runs/discover", json={"root_url": "https://x.test/"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["branches"][0]["prefix"], "/docs/")

    def test_discover_requires_root_url(self):
        r = self.client.post("/runs/discover", json={})
        self.assertEqual(r.status_code, 400)

    def test_start_run_then_poll_job_to_completion(self):
        r = self.client.post("/runs/start", json={"mode": "map", "root_url": "https://x.test/"})
        self.assertEqual(r.status_code, 202)
        body = r.get_json()
        run_id = body["run_id"]
        self.assertEqual(body["job"]["state"], "running")

        import time
        for _ in range(50):
            r = self.client.get(f"/runs/{run_id}/job")
            if r.get_json()["job"]["state"] != "running":
                break
            time.sleep(0.05)
        job = r.get_json()["job"]
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["counts_by_status"]["mapped"], 2)

    def test_start_run_rejects_unknown_mode(self):
        r = self.client.post("/runs/start", json={"mode": "bogus", "root_url": "https://x.test/"})
        self.assertEqual(r.status_code, 400)

    def test_starting_twice_with_same_run_id_conflicts(self):
        r1 = self.client.post("/runs/start", json={"mode": "map", "root_url": "https://x.test/", "run_id": "run-dup"})
        self.assertEqual(r1.status_code, 202)
        r2 = self.client.post("/runs/start", json={"mode": "map", "root_url": "https://x.test/", "run_id": "run-dup"})
        self.assertEqual(r2.status_code, 409)
        # Let the fake subprocess finish (it exits after ~0.3s) so it
        # releases job.log before tempdir cleanup -- Windows won't unlink
        # an open file.
        import time
        time.sleep(0.5)

    def test_resume_without_prior_config_fails_cleanly(self):
        self._make_run("run-b", {"mapped": 1})
        r = self.client.post("/runs/run-b/resume")
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_resume_can_promote_a_mapped_run_to_a_real_crawl(self):
        """The actual map-then-select-then-crawl workflow: resume with a
        mode override must relaunch as a crawl against the SAME run_dir,
        not start a fresh run."""
        r = self.client.post("/runs/start", json={
            "mode": "map", "root_url": "https://x.test/", "run_id": "run-promote",
        })
        self.assertEqual(r.status_code, 202)
        import time
        for _ in range(50):
            job = self.client.get("/runs/run-promote/job").get_json()["job"]
            if job["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done")

        # The fake main.py stand-in doesn't create a real frontier.db (the
        # real headless_map does, via Frontier.open(), almost
        # immediately) -- touch one so _require_run's existence check
        # passes, same as it would against the real subprocess.
        import sqlite3
        sqlite3.connect(os.path.join(self._tmp.name, "run-promote", "frontier.db")).close()

        r = self.client.post("/runs/run-promote/resume", json={"mode": "crawl", "provider": "openai"})
        self.assertEqual(r.status_code, 202)
        with open(os.path.join(self._tmp.name, "run-promote", "run_config.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["mode"], "crawl")
        self.assertEqual(saved["run_dir"], os.path.join(self._tmp.name, "run-promote"))
        time.sleep(0.5)

    def test_stop_with_no_job_404s(self):
        self._make_run("run-c", {"mapped": 1})
        r = self.client.post("/runs/run-c/stop")
        self.assertEqual(r.status_code, 404)

    def test_pause_and_unpause(self):
        self._make_run("run-d", {"queued": 1})
        r = self.client.post("/runs/run-d/pause")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["paused"])
        r = self.client.post("/runs/run-d/unpause")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["paused"])

    # -- cancel / select ---------------------------------------------------

    def test_cancel_requires_url_or_prefix(self):
        self._make_run("run-e", {"queued": 1})
        r = self.client.post("/runs/run-e/cancel", json={})
        self.assertEqual(r.status_code, 400)

    def test_cancel_and_select_happy_path(self):
        self._make_run("run-f", {"queued": 1})
        r = self.client.post("/runs/run-f/cancel", json={"url": "https://x.test/1"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/runs/run-f/select", json={"prefix": "https://x.test/"})
        self.assertEqual(r.status_code, 200)

    # -- read-only: graph / live-status / dataset-report / query --------

    def test_graph_discovery(self):
        run_dir = self._make_run("run-g", {"mapped": 2})
        r = self.client.get("/runs/run-g/graph?type=discovery")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["type"], "discovery_graph")

    def test_graph_invalid_type(self):
        self._make_run("run-h", {"mapped": 1})
        r = self.client.get("/runs/run-h/graph?type=bogus")
        self.assertEqual(r.status_code, 400)

    def test_live_status(self):
        self._make_run("run-i", {"queued": 1, "in_progress": 1})
        r = self.client.get("/runs/run-i/live-status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["counts_by_status"]["queued"], 1)

    def test_dataset_report_requires_canonical_file(self):
        self._make_run("run-j", {"mapped": 1})
        r = self.client.get("/runs/run-j/dataset-report")
        self.assertEqual(r.status_code, 404)

    def test_dataset_report_with_canonical_file(self):
        run_dir = self._make_run("run-k", {"done": 1})
        with open(os.path.join(run_dir, "canonical.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"question": "q", "answer": "a"}\n')
        r = self.client.get("/runs/run-k/dataset-report")
        self.assertEqual(r.status_code, 200)
        self.assertIn("report_text", r.get_json())

    def test_query_requires_rag_index(self):
        self._make_run("run-l", {"done": 1})
        r = self.client.post("/runs/run-l/query", json={"question": "what?"})
        self.assertEqual(r.status_code, 404)

    def test_query_with_rag_dir_present(self):
        run_dir = self._make_run("run-m", {"done": 1})
        os.makedirs(os.path.join(run_dir, "chroma_db"))
        r = self.client.post("/runs/run-m/query", json={"question": "what?"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("report_text", r.get_json())

    # -- errors are always structured JSON -------------------------------

    def test_unknown_route_is_json_404(self):
        r = self.client.get("/nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.content_type, "application/json")


if __name__ == "__main__":
    unittest.main()
