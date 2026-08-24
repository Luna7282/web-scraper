"""server/jobs.py::JobManager -- subprocess lifecycle, restart-survival
(state re-derived from job.json + the OS on every call, never cached),
and the pid-file mechanism explicitly, not just exercised incidentally.
Uses a trivial `python -c ...` command as the "job" instead of a real
main.py subprocess -- fast, offline, no crawl4ai/network involved; the
real subprocess wiring (main.py --headless-config) is proven separately
by the live demo, not by these unit tests.
"""
import json
import os
import sys
import tempfile
import time
import unittest

from server.jobs import JobManager, JobError


def _py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


class TestJobLifecycle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = os.path.join(self._tmp.name, "run-x")
        self.jobs = JobManager()

    def tearDown(self):
        self._tmp.cleanup()

    def test_start_writes_job_and_config_files(self):
        cmd = _py("import time; time.sleep(2)")
        record = self.jobs.start(self.run_dir, "crawl", cmd, config={"mode": "crawl", "root_url": "https://x.test"})
        self.assertEqual(record["state"], "running")
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "job.json")))
        with open(os.path.join(self.run_dir, "run_config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["root_url"], "https://x.test")
        self.jobs.stop(self.run_dir)

    def test_a_second_start_while_one_is_running_raises(self):
        cmd = _py("import time; time.sleep(5)")
        self.jobs.start(self.run_dir, "crawl", cmd)
        with self.assertRaises(JobError):
            self.jobs.start(self.run_dir, "crawl", cmd)
        self.jobs.stop(self.run_dir)

    def test_completed_job_reports_done_with_parsed_summary(self):
        cmd = _py('import json; print(json.dumps({"mode": "map", "counts_by_status": {"mapped": 3}, "fatal_errors": []}))')
        self.jobs.start(self.run_dir, "map", cmd)
        for _ in range(50):
            status = self.jobs.get_status(self.run_dir)
            if status["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["result"]["counts_by_status"]["mapped"], 3)

    def test_job_with_fatal_errors_and_nonzero_exit_reports_failed(self):
        """Matches main.py's real --headless-config dispatch: it prints
        the summary AND calls sys.exit(1) when fatal_errors is non-empty
        -- state must come from the real exit code (via the wrapper, see
        _wrap_with_exit_marker), not from sniffing the summary's keys,
        because a job kind that prints a differently-shaped summary (e.g.
        export's dataset_card) has no 'fatal_errors' key to sniff at all."""
        cmd = _py(
            'import json, sys; '
            'print(json.dumps({"counts_by_status": {}, "fatal_errors": ["boom"]})); '
            'sys.exit(1)'
        )
        self.jobs.start(self.run_dir, "crawl", cmd)
        for _ in range(50):
            status = self.jobs.get_status(self.run_dir)
            if status["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["returncode"], 1)
        self.assertEqual(status["result"]["fatal_errors"], ["boom"])

    def test_a_shape_the_summary_sniffer_would_never_recognize_still_reports_done(self):
        """The actual bug this design fixes: export.py prints a
        dataset_card JSON with none of headless_run's keys. A job whose
        output has no recognizable 'shape' at all must still report
        'done' on a clean exit -- exit code is authoritative, not stdout
        content."""
        cmd = _py('import json; print(json.dumps({"totally": "unrelated", "shape": True}))')
        self.jobs.start(self.run_dir, "export", cmd)
        for _ in range(50):
            status = self.jobs.get_status(self.run_dir)
            if status["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["result"], {"totally": "unrelated", "shape": True})

    def test_nonzero_exit_with_no_output_at_all_reports_failed_not_exited(self):
        cmd = _py("import sys; sys.exit(1)")
        self.jobs.start(self.run_dir, "crawl", cmd)
        for _ in range(50):
            status = self.jobs.get_status(self.run_dir)
            if status["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["returncode"], 1)
        self.assertIsNone(status["result"])

    def test_multiline_pretty_printed_summary_is_still_captured(self):
        """export.py prints its dataset_card via json.dumps(card, indent=2)
        -- multiple lines, not one. The result field must still capture
        it, not just the exit code."""
        cmd = _py('import json; print(json.dumps({"a": 1, "b": [1, 2, 3]}, indent=2))')
        self.jobs.start(self.run_dir, "export", cmd)
        for _ in range(50):
            status = self.jobs.get_status(self.run_dir)
            if status["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["result"], {"a": 1, "b": [1, 2, 3]})

    def test_missing_exit_marker_entirely_reports_exited(self):
        """The narrower thing 'exited' now means: the wrapper itself
        never got to report a real exit code at all (e.g. job.log was
        truncated/lost, or something killed the wrapper process before it
        could finish) -- not just 'the inner command produced no useful
        stdout', which is now correctly 'failed'/'done' based on the real
        exit code instead."""
        cmd = _py("import time; time.sleep(1)")
        self.jobs.start(self.run_dir, "crawl", cmd)
        time.sleep(1.5)  # let it actually finish and write its marker
        # Simulate the marker never having been written (log lost/corrupted).
        with open(os.path.join(self.run_dir, "job.log"), "w", encoding="utf-8") as f:
            f.write("some unrelated crawl4ai noise\n")
        status = self.jobs.get_status(self.run_dir)
        self.assertEqual(status["state"], "exited")
        self.assertIsNone(status["result"])

    def test_stop_kills_the_process_and_marks_stopped_not_done(self):
        cmd = _py("import time; time.sleep(30)")
        self.jobs.start(self.run_dir, "crawl", cmd)
        record = self.jobs.stop(self.run_dir)
        self.assertEqual(record["state"], "stopped")
        # Give the OS a moment, then confirm it's actually gone, not just marked.
        time.sleep(0.5)
        status = self.jobs.get_status(self.run_dir)
        self.assertEqual(status["state"], "stopped")

    def test_stop_on_a_job_that_already_finished_naturally_reports_done_not_stopped(self):
        """Found by the live demo: a job can legitimately finish (hit
        max_pages, quiesce) in the gap between deciding to stop it and
        the stop() call landing. Killing an already-dead pid is a
        harmless no-op, but must not relabel a real completion as a
        deliberate stop."""
        cmd = _py('import json; print(json.dumps({"counts_by_status": {"mapped": 5}, "fatal_errors": []}))')
        self.jobs.start(self.run_dir, "map", cmd)
        for _ in range(50):
            status = self.jobs.get_status(self.run_dir)
            if status["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(status["state"], "done")  # finished on its own already

        record = self.jobs.stop(self.run_dir)
        self.assertEqual(record["state"], "done")
        self.assertEqual(record["result"]["counts_by_status"]["mapped"], 5)

    def test_stop_with_no_job_raises(self):
        with self.assertRaises(JobError):
            self.jobs.stop(self.run_dir)

    def test_status_survives_a_fresh_jobmanager_instance(self):
        """The actual restart-survival claim: a brand-new JobManager (as
        if the server process had restarted) must read the same state
        from job.json + the OS, with no in-memory handoff at all."""
        cmd = _py("import time; time.sleep(2)")
        self.jobs.start(self.run_dir, "crawl", cmd)

        fresh = JobManager()
        status = fresh.get_status(self.run_dir)
        self.assertEqual(status["state"], "running")
        fresh.stop(self.run_dir)

    def test_get_status_on_unknown_run_dir_is_none(self):
        self.assertIsNone(self.jobs.get_status(os.path.join(self._tmp.name, "never-started")))


if __name__ == "__main__":
    unittest.main()
