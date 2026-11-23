"""Headless contract v1, Brief 3A Step 2: result.json for every outcome, the
scraper version, the run_dir resume rule, run ids on timing events, and
legacy output left exactly as before. Offline: an injected runner stands in
for headless_run; real temp run_dirs; no crawl, no network.
"""

import asyncio
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import headless
import main
from crawl.timing import TimingLog
from storage.canonical import build_canonical_record
from tests.test_headless_contract import SECRET, ConfigGuard

VERSION = {"commit": "abc1234", "dirty": False, "version": "abc1234"}
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
LEGACY_KEYS = ["question", "answer", "source_chunk", "chunk_index", "source_url", "section", "page_title",
               "generation_model", "extraction_strategy", "timestamp", "crawl_date", "license_signal",
               "quality_score", "quality_decision_method"]


def ok_result(counts=None):
    return {"mode": "crawl", "counts_by_status": counts or {"done": 2}, "fatal_errors": []}


class RunDirCase(ConfigGuard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()

    def tearDown(self):
        os.chmod(self.run_dir, 0o755) if self.run_dir.exists() else None
        self.tmp.cleanup()
        super().tearDown()

    def cfg(self, **overrides):
        cfg = {"contract_version": 1, "mode": "crawl", "run_dir": str(self.run_dir),
               "seeds": [["https://example.test/", None]], "host_prefix_pairs": [["example.test", "/"]]}
        cfg.update(overrides)
        return cfg

    def entry(self, cfg, run, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = main.headless_v1_entry(cfg, run=run, out=out, err=err, environ=environ or {}, version=VERSION)
        return code, out.getvalue(), err.getvalue()

    def result(self, name="result.json"):
        return json.loads((self.run_dir / name).read_text())


class TestResultPerOutcome(RunDirCase):
    def test_ok(self):
        seen = {}

        async def run(cfg, api_key=None, context=None):
            seen["context"] = context
            return ok_result()

        code, out, _ = self.entry(self.cfg(run_id="run-ok", settings={"extract_unit_concurrency": 4},
                                           api_key_env="RUN_KEY"), run, environ={"RUN_KEY": SECRET})
        r = self.result()
        self.assertEqual(code, 0)
        self.assertEqual((r["contract_version"], r["run_id"], r["mode"], r["outcome"], r["exit_code"]),
                         (1, "run-ok", "crawl", "ok", 0))
        self.assertEqual((r["scraper_version"], r["scraper_commit"], r["scraper_dirty"]), ("abc1234", "abc1234", False))
        self.assertEqual(r["settings"]["extract_unit_concurrency"], {"value": 4, "source": "config"})
        self.assertEqual(r["settings"]["llm_sdk_max_retries"]["source"], "default")
        self.assertEqual((r["counts_by_status"], r["fatal_errors"]), ({"done": 2}, []))
        self.assertEqual(r["api_key_env"], {"name": "RUN_KEY", "set": True})
        self.assertTrue(r["started_at"] and r["finished_at"])
        self.assertNotIn("error", r)
        self.assertNotIn("resumed_from_run_id", r)
        self.assertEqual(json.loads(out), r)  # the stdout line is the same result
        self.assertNotIn(SECRET, out + (self.run_dir / "result.json").read_text())
        self.assertEqual((seen["context"].run_id, seen["context"].scraper_version), ("run-ok", "abc1234"))

    def test_run_id_is_generated_when_absent(self):
        async def run(cfg, api_key=None, context=None):
            return ok_result()

        self.entry(self.cfg(), run)
        self.assertRegex(self.result()["run_id"], RUN_ID)

    def test_stopped_early(self):
        async def run(cfg, api_key=None, context=None):
            return {"mode": "crawl", "counts_by_status": {"failed": 1}, "fatal_errors": ["[FATAL] breaker stop"]}

        code, out, _ = self.entry(self.cfg(), run)
        r = self.result()
        self.assertEqual((code, r["outcome"], r["exit_code"], r["fatal_errors"]),
                         (1, "stopped_early", 1, ["[FATAL] breaker stop"]))
        self.assertEqual(json.loads(out)["outcome"], "stopped_early")

    def test_interrupted(self):
        async def run(cfg, api_key=None, context=None):
            raise KeyboardInterrupt

        code, out, _ = self.entry(self.cfg(), run)
        self.assertEqual((code, self.result()["outcome"], self.result()["exit_code"]), (130, "interrupted", 130))
        self.assertEqual(json.loads(out)["exit_code"], 130)

    def test_injected_crash_records_type_and_a_clean_message(self):
        async def run(cfg, api_key=None, context=None):
            raise RuntimeError(f"cannot open /Users/someone/private/run/frontier.db using {SECRET}")

        code, out, err = self.entry(self.cfg(), run)
        r = self.result()
        self.assertEqual((code, r["outcome"], r["exit_code"]), (1, "crashed", 1))
        self.assertEqual(r["error"]["type"], "RuntimeError")
        self.assertIn("<path>", r["error"]["message"])
        self.assertIn("[redacted]", r["error"]["message"])
        text = (self.run_dir / "result.json").read_text() + out + err
        self.assertNotIn("/Users/someone", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Traceback", err)
        self.assertEqual(err.count("\n"), 1)
        self.assertEqual(json.loads(out)["outcome"], "crashed")

    def test_long_crash_message_is_capped(self):
        async def run(cfg, api_key=None, context=None):
            raise ValueError("x" * 5000)

        self.entry(self.cfg(), run)
        self.assertLessEqual(len(self.result()["error"]["message"]), headless.ERROR_MESSAGE_MAX)

    def test_config_error_in_an_existing_empty_run_dir(self):
        async def must_not_run(cfg, api_key=None, context=None):
            raise AssertionError("ran despite a config error")

        code, out, err = self.entry(self.cfg(max_pagez=5), must_not_run)
        r = self.result()
        self.assertEqual((code, out), (2, ""))  # config errors: message on stderr, nothing on stdout
        self.assertEqual((r["outcome"], r["exit_code"]), ("config_error", 2))
        self.assertEqual(r["error"], {"type": "HeadlessConfigError", "message": "unknown config keys: max_pagez"})
        self.assertEqual(err, "config error: unknown config keys: max_pagez\n")

    def test_config_error_never_creates_a_missing_run_dir(self):
        missing = Path(self.tmp.name) / "never-created"

        async def must_not_run(cfg, api_key=None, context=None):
            raise AssertionError("ran")

        code, _, _ = self.entry(self.cfg(run_dir=str(missing), max_pagez=1), must_not_run)
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())


class TestUnwritableRunDir(RunDirCase):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anywhere")
    def test_exit_code_and_stdout_line_survive_an_unwritable_run_dir(self):
        async def run(cfg, api_key=None, context=None):
            return ok_result()

        os.chmod(self.run_dir, 0o500)  # readable, not writable
        code, out, err = self.entry(self.cfg(), run)
        os.chmod(self.run_dir, 0o755)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["outcome"], "ok")
        self.assertIn("could not write result.json", err)
        self.assertFalse((self.run_dir / "result.json").exists())
        self.assertEqual(list(self.run_dir.iterdir()), [])  # no stray temp file

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anywhere")
    def test_a_crash_in_an_unwritable_run_dir_still_exits_1_with_a_line(self):
        async def run(cfg, api_key=None, context=None):
            raise RuntimeError("boom")

        os.chmod(self.run_dir, 0o500)
        code, out, _ = self.entry(self.cfg(), run)
        os.chmod(self.run_dir, 0o755)
        self.assertEqual((code, json.loads(out)["outcome"]), (1, "crashed"))


class TestRunDirRule(RunDirCase):
    async def _ok(self, cfg, api_key=None, context=None):
        return ok_result()

    def test_each_state_file_alone_refuses_a_fresh_run(self):
        for name in headless.STATE_FILES:
            with self.subTest(state_file=name):
                for f in self.run_dir.iterdir():
                    f.unlink()
                (self.run_dir / name).write_text("{}" if name == "result.json" else "")
                before = (self.run_dir / name).read_text()
                code, out, err = self.entry(self.cfg(), self._ok)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(name, err)
                self.assertIn('"resume": true', err)
                self.assertEqual((self.run_dir / name).read_text(), before)  # nothing overwritten

    def test_non_state_files_do_not_count(self):
        """foundry's adapter writes these into the run_dir before launching."""
        (self.run_dir / "headless_config.json").write_text("{}")
        (self.run_dir / "run.log").write_text("log")
        code, _, _ = self.entry(self.cfg(), self._ok)
        self.assertEqual(code, 0)
        self.assertEqual(self.result()["outcome"], "ok")

    def test_resume_with_nothing_to_resume_is_refused(self):
        code, out, err = self.entry(self.cfg(resume=True), self._ok)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no scraper run to resume", err)
        self.assertEqual(self.result()["outcome"], "config_error")  # empty dir: safe to record


class TestResume(RunDirCase):
    def run_writing_timings(self, n):
        """A stand-in run that, like headless_crawl, writes timing events with
        the context's run id."""
        async def run(cfg, api_key=None, context=None):
            log = TimingLog(str(self.run_dir / "timings.jsonl"), provider="p", model="m", run_id=context.run_id)
            log.start()
            for i in range(n):
                log.record("page", url=f"https://example.test/{i}")
            await log.close()
            return ok_result()
        return run

    def test_resume_archives_the_old_result_and_each_event_keeps_its_run_id(self):
        self.assertEqual(self.entry(self.cfg(run_id="run-a"), self.run_writing_timings(2))[0], 0)
        code, out, _ = self.entry(self.cfg(run_id="run-b", resume=True), self.run_writing_timings(1))
        self.assertEqual(code, 0)
        new, old = self.result(), self.result("result.run-a.json")
        self.assertEqual((new["run_id"], new["resumed_from_run_id"]), ("run-b", "run-a"))
        self.assertEqual((old["run_id"], old["outcome"]), ("run-a", "ok"))
        self.assertNotIn("resumed_from_run_id", old)
        events = [json.loads(line) for line in (self.run_dir / "timings.jsonl").read_text().splitlines()]
        self.assertEqual([e["run_id"] for e in events],
                         ["run-a", "run-a", "run-a", "run-b", "run-b"])  # 2 pages + run_end, then 1 + run_end
        self.assertEqual(json.loads(out)["resumed_from_run_id"], "run-a")

    def test_resume_without_a_previous_result_records_unknown(self):
        (self.run_dir / "frontier.db").write_text("")
        self.entry(self.cfg(resume=True), self.run_writing_timings(1))
        self.assertEqual(self.result()["resumed_from_run_id"], "unknown")
        self.assertEqual(sorted(p.name for p in self.run_dir.glob("result.*.json")), [])

    def test_an_unreadable_previous_result_is_kept_as_unknown(self):
        (self.run_dir / "result.json").write_text("{not json")
        self.entry(self.cfg(resume=True), self.run_writing_timings(1))
        self.assertEqual(self.result()["resumed_from_run_id"], "unknown")
        (archived,) = self.run_dir.glob("result.unknown-*.json")
        self.assertEqual(archived.read_text(), "{not json")  # kept, not overwritten

    def test_an_existing_archive_is_never_overwritten(self):
        self.entry(self.cfg(run_id="run-a"), self.run_writing_timings(1))
        (self.run_dir / "result.run-a.json").write_text("earlier archive")
        self.entry(self.cfg(run_id="run-b", resume=True), self.run_writing_timings(1))
        self.assertEqual((self.run_dir / "result.run-a.json").read_text(), "earlier archive")
        self.assertEqual(len(list(self.run_dir.glob("result.run-a-*.json"))), 1)


class TestScraperVersion(unittest.TestCase):
    def test_real_checkout_reports_its_commit(self):
        v = headless.scraper_version()
        self.assertRegex(v["commit"], r"^[0-9a-f]{7,}$")
        self.assertIsInstance(v["dirty"], bool)
        self.assertEqual(v["version"], v["commit"] + ("+dirty" if v["dirty"] else ""))

    def test_fallback_when_git_cannot_answer(self):
        def failing(*a, **k):
            return type("Done", (), {"returncode": 128, "stdout": ""})()

        def missing(*a, **k):
            raise FileNotFoundError("git")

        for run in (failing, missing):
            with self.subTest(run=run.__name__):
                self.assertEqual(headless.scraper_version(run=run),
                                 {"commit": None, "dirty": None, "version": "0.1.0+unknown"})


ROW_KW = {"question": "Q?", "answer": "A.", "source_chunk": "c", "chunk_index": 0, "source_url": "https://x.test/a",
          "section": "s", "page_title": "T", "generation_model": "m", "extraction_strategy": "per_chunk",
          "timestamp": "2026-11-23T00:00:00+00:00", "crawl_date": "2026-11-23", "license_signal": None}


class TestRowsAndEvents(unittest.TestCase):

    def test_v1_rows_carry_scraper_version(self):
        row = build_canonical_record(**ROW_KW, scraper_version="abc1234+dirty")
        self.assertEqual(row["scraper_version"], "abc1234+dirty")
        self.assertEqual(list(row), LEGACY_KEYS + ["scraper_version"])

    def test_legacy_rows_are_unchanged(self):
        """Byte-identical to the pre-Step-2 code was checked against git HEAD
        when this was written; this pins the exact keys and their order."""
        self.assertEqual(list(build_canonical_record(**ROW_KW)), LEGACY_KEYS)

    def test_extract_worker_passes_scraper_version_into_rows(self):
        from crawl.frontier import Frontier
        from crawl.pipeline import extract_worker

        async def scenario(version):
            f = Frontier(":memory:", max_retries=3)
            await f.open()
            await f.recover_crashed()
            await f.seed([("https://x.test/a", None)])
            cq, rq = asyncio.Queue(maxsize=4), asyncio.Queue(maxsize=4)

            async def score(url, content):
                return 1.0

            async def extract(unit):
                return '[{"instruction": "Q", "response": "A"}]'

            async def recrawl():
                while True:
                    row = await f.claim()
                    if row is None:
                        await asyncio.sleep(0.01)
                        continue
                    await f.put_content(cq, (row.url, "content"))

            tasks = [asyncio.create_task(extract_worker(f, cq, rq, score, extract, 0.5, 0.5, poll_interval=0.01,
                                                        scraper_version=version)),
                     asyncio.create_task(recrawl())]
            result = await asyncio.wait_for(rq.get(), 5)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await f.close()
            return result.qa_pairs[0]

        self.assertEqual(asyncio.run(scenario("abc1234"))["scraper_version"], "abc1234")
        self.assertNotIn("scraper_version", asyncio.run(scenario(None)))

    def test_timing_events_carry_run_id_only_when_given(self):
        async def log_one(path, **kw):
            log = TimingLog(path, **kw)
            log.start()
            log.record("page", url="u")
            await log.close()

        def first_event(**kw):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "t.jsonl")
                asyncio.run(log_one(path, **kw))
                with open(path, encoding="utf-8") as f:
                    return json.loads(f.readline())

        v1_event, legacy_event = first_event(run_id="run-a"), first_event()
        self.assertEqual(list(v1_event)[:3], ["event", "ts", "run_id"])
        self.assertEqual(v1_event["run_id"], "run-a")
        self.assertEqual(list(legacy_event), ["event", "ts", "url"])  # legacy: exactly as before


if __name__ == "__main__":
    unittest.main()
