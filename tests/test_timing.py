"""Opt-in timing log (crawl/timing.py, config.EXTRACT_TIMING_LOG) -- offline,
stub fetch/score/extract functions, temp files only, no network, no LLM.

Covers: TimingLog itself (JSON lines, append-across-runs, run_end, a bad
path never raises), the events crawl_worker/extract_worker emit when a
TimingLog is passed, and that the default (timing=None / flag off) path
produces the same pipeline output as before.
"""
import asyncio
import json
import os
import tempfile
import unittest

from crawl.frontier import Frontier
from crawl.pipeline import (
    ExtractionResult,
    RateLimitError,
    crawl_worker,
    extract_worker,
)
from crawl.timing import TimingLog, call_usage, usage_from_message
from decisions.types import NoulAnswer

SEED = "https://x.test/a"
CONTENT = "markdown content"
UNITS = ["unit zero", "unit one -- malformed", "unit two!"]


def read_events(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


async def recrawl_stub(frontier, content_queue, content, poll_interval=0.01):
    while True:
        row = await frontier.claim()
        if row is None:
            if frontier.quiescent.is_set():
                return
            await asyncio.sleep(poll_interval)
            continue
        await frontier.put_content(content_queue, (row.url, content))


async def cancel_all(tasks):
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def score_fn(url, content):
    return 1.0


async def units_fn(content):
    return list(UNITS)


async def extract_fn(unit):
    await asyncio.sleep(0.01)
    if "malformed" in unit:
        return "not json at all"
    return json.dumps([{"instruction": f"Q {unit}", "response": f"A {unit}"}])


class StubQualityBackend:
    async def noul(self, text, question):
        return NoulAnswer(value=0.9, method="traditional")


class TestTimingLog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "run", "timings.jsonl")  # parent dir doesn't exist yet

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_records_are_written_as_json_lines_with_run_end(self):
        log = TimingLog(self.path, provider="gemini", model="m")
        log.start()
        log.record("extract_call", url=SEED, seconds=0.5, **log.llm_fields())
        log.record("page", url=SEED, units=1)
        await log.close()

        events = read_events(self.path)
        self.assertEqual([e["event"] for e in events], ["extract_call", "page", "run_end"])
        self.assertEqual(events[0]["provider"], "gemini")
        self.assertEqual(events[0]["model"], "m")
        self.assertIn("ts", events[0])
        self.assertGreaterEqual(events[-1]["wall_seconds"], 0)

    async def test_second_run_appends_rather_than_truncating(self):
        for _ in range(2):
            log = TimingLog(self.path)
            log.start()
            log.record("run_start")
            await log.close()
        self.assertEqual(
            [e["event"] for e in read_events(self.path)],
            ["run_start", "run_end", "run_start", "run_end"],
        )

    async def test_unwritable_path_warns_but_never_raises(self):
        bad_path = self.tmp.name  # a directory, not a file -- open(..., "a") fails
        log = TimingLog(bad_path)
        log.start()
        log.record("page", url=SEED)
        await log.close()  # must not raise

    async def test_queue_wait_is_paired_by_url_and_consumed_once(self):
        log = TimingLog(self.path)
        log.mark_enqueued(SEED)
        await asyncio.sleep(0.02)
        wait = log.pop_queue_wait(SEED)
        self.assertGreaterEqual(wait, 0.015)
        self.assertIsNone(log.pop_queue_wait(SEED))
        self.assertIsNone(log.pop_queue_wait("https://x.test/never-enqueued"))


class TestPipelineTimingEvents(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "timings.jsonl")
        self.frontier = Frontier(":memory:", max_retries=3)
        await self.frontier.open()
        await self.frontier.seed([(SEED, None)])
        self.content_queue = asyncio.Queue(maxsize=4)
        self.results_queue = asyncio.Queue(maxsize=4)

    async def asyncTearDown(self):
        await self.frontier.close()
        self.tmp.cleanup()

    async def _run_extract(self, timing, extract=extract_fn, quality_backend=None):
        tasks = [
            asyncio.create_task(extract_worker(
                self.frontier, self.content_queue, self.results_queue, score_fn, extract,
                0.5, 0.5, extraction_units_fn=units_fn, poll_interval=0.01,
                quality_backend=quality_backend, quality_threshold=0.5, timing=timing,
            )),
            asyncio.create_task(recrawl_stub(self.frontier, self.content_queue, CONTENT)),
        ]
        return tasks

    async def _wait_for_result(self, tasks):
        for _ in range(200):
            if self.results_queue.qsize() >= 1:
                break
            await asyncio.sleep(0.01)
        await cancel_all(tasks)
        return self.results_queue.get_nowait()

    async def test_extract_worker_emits_call_quality_score_and_page_events(self):
        timing = TimingLog(self.path, provider="gemini", model="gemini-test")
        timing.start()
        tasks = await self._run_extract(timing, quality_backend=StubQualityBackend())
        result: ExtractionResult = await self._wait_for_result(tasks)
        await timing.close()

        events = read_events(self.path)
        calls = [e for e in events if e["event"] == "extract_call"]
        self.assertEqual([c["chunk_index"] for c in calls], [0, 1, 2])
        self.assertEqual([c["outcome"] for c in calls], ["ok", "malformed", "ok"])
        self.assertEqual([c["chars_in"] for c in calls], [len(u) for u in UNITS])
        for c in calls:
            self.assertEqual((c["url"], c["provider"], c["model"]), (SEED, "gemini", "gemini-test"))
            self.assertGreaterEqual(c["seconds"], 0.005)  # the stub's sleep, not parse time

        quality = [e for e in events if e["event"] == "quality_call"]
        self.assertEqual([(q["chunk_index"], q["outcome"]) for q in quality], [(0, "ok"), (2, "ok")])

        (score,) = [e for e in events if e["event"] == "score"]
        self.assertEqual(score["url"], SEED)
        self.assertIsNone(score["queue_wait_seconds"])  # recrawl_stub doesn't mark_enqueued

        (page,) = [e for e in events if e["event"] == "page"]
        self.assertEqual((page["outcome"], page["units"], page["pairs"]), ("ok", 3, 2))
        self.assertEqual(len(result.qa_pairs), 2)
        self.assertGreaterEqual(page["extraction_seconds"], sum(c["seconds"] for c in calls) * 0.9)

    async def test_rate_limited_call_and_page_are_labelled(self):
        rate_limited = asyncio.Event()

        async def rate_limited_extract(unit):
            if unit == UNITS[1]:
                rate_limited.set()
                raise RateLimitError(retry_after=60)  # long backoff: no retry inside this test
            return await extract_fn(unit)

        timing = TimingLog(self.path)
        timing.start()
        tasks = await self._run_extract(timing, extract=rate_limited_extract)
        await asyncio.wait_for(rate_limited.wait(), timeout=5)
        await asyncio.sleep(0.05)  # let extract_worker finish handling the exception
        await cancel_all(tasks)
        await timing.close()

        events = read_events(self.path)
        calls = [e for e in events if e["event"] == "extract_call"]
        self.assertEqual([c["outcome"] for c in calls[:2]], ["ok", "rate_limited"])
        pages = [e for e in events if e["event"] == "page"]
        self.assertEqual(pages[0]["outcome"], "rate_limited")
        self.assertEqual(pages[0]["units"], 3)

    async def test_crawl_worker_emits_fetch_and_queue_wait(self):
        async def fetch_fn(url):
            await asyncio.sleep(0.01)
            return CONTENT, []

        timing = TimingLog(self.path)
        timing.start()
        tasks = [
            asyncio.create_task(crawl_worker(
                self.frontier, fetch_fn, self.content_queue, lambda u: True,
                poll_interval=0.01, timing=timing,
            )),
        ]
        for _ in range(200):
            if self.content_queue.qsize() >= 1:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)  # page sits in content_queue before an extract worker exists
        tasks += await self._run_extract(timing)
        await self._wait_for_result(tasks)
        await timing.close()

        events = read_events(self.path)
        (fetch,) = [e for e in events if e["event"] == "fetch"]
        self.assertEqual((fetch["url"], fetch["outcome"]), (SEED, "ok"))
        self.assertGreaterEqual(fetch["seconds"], 0.005)
        (score,) = [e for e in events if e["event"] == "score"]
        self.assertGreaterEqual(score["queue_wait_seconds"], 0.015)

    async def test_failed_fetch_is_recorded_with_exception_name(self):
        async def fetch_fn(url):
            raise ValueError("boom")

        timing = TimingLog(self.path)
        timing.start()
        task = asyncio.create_task(crawl_worker(
            self.frontier, fetch_fn, self.content_queue, lambda u: True,
            poll_interval=0.01, timing=timing,
        ))
        for _ in range(200):
            if (await self.frontier.counts_by_status()).get("failed"):
                break
            await asyncio.sleep(0.01)
        await cancel_all([task])
        await timing.close()
        fetches = [e for e in read_events(self.path) if e["event"] == "fetch"]
        self.assertTrue(fetches)
        self.assertEqual(fetches[0]["outcome"], "ValueError")

    async def test_timing_none_gives_identical_pipeline_output(self):
        """The default path: same qa_pairs whether timing is off or on."""
        async def run(timing):
            frontier = Frontier(":memory:", max_retries=3)
            await frontier.open()
            await frontier.seed([(SEED, None)])
            cq, rq = asyncio.Queue(maxsize=4), asyncio.Queue(maxsize=4)
            tasks = [
                asyncio.create_task(extract_worker(
                    frontier, cq, rq, score_fn, extract_fn, 0.5, 0.5,
                    extraction_units_fn=units_fn, poll_interval=0.01,
                    quality_backend=StubQualityBackend(), quality_threshold=0.5, timing=timing,
                )),
                asyncio.create_task(recrawl_stub(frontier, cq, CONTENT)),
            ]
            for _ in range(200):
                if rq.qsize() >= 1:
                    break
                await asyncio.sleep(0.01)
            await cancel_all(tasks)
            result = rq.get_nowait()
            await frontier.close()
            strip = ("timestamp", "crawl_date")
            return [{k: v for k, v in p.items() if k not in strip} for p in result.qa_pairs]

        timing = TimingLog(self.path)
        timing.start()
        with_timing = await run(timing)
        await timing.close()
        self.assertEqual(await run(None), with_timing)


class TestTimingOffByDefault(unittest.TestCase):
    def test_flag_defaults_false_without_env_override(self):
        # Fresh interpreter, not importlib.reload(config): reloading would
        # recreate config's Enum classes under every other test module.
        import subprocess
        import sys
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if k != "EXTRACT_TIMING_LOG"}
        out = subprocess.run(
            [sys.executable, "-c",
             "import config; print(config.EXTRACT_TIMING_LOG, config.EXTRACT_TIMING_LOG_FILENAME)"],
            cwd=repo_root, env=env, capture_output=True, text=True, timeout=60, check=True,
        )
        self.assertEqual(out.stdout.split(), ["False", "timings.jsonl"])

    def test_main_builds_no_timing_log_when_flag_off(self):
        from unittest.mock import patch

        import main
        with patch.object(main.config, "EXTRACT_TIMING_LOG", False):
            self.assertIsNone(main._start_timing_log("/nonexistent", "gemini", "m", 1, 1, None, False))
        self.assertFalse(os.path.exists("/nonexistent"))



class TestTokenUsageCapture(unittest.IsolatedAsyncioTestCase):
    """Usage is published by extract_fn via crawl.timing.call_usage and
    attached to that call's extract_call event -- never a previous call's."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "timings.jsonl")
        self.frontier = Frontier(":memory:", max_retries=3)
        await self.frontier.open()
        await self.frontier.seed([(SEED, None)])

    async def asyncTearDown(self):
        await self.frontier.close()
        self.tmp.cleanup()

    async def test_usage_attached_per_call_and_not_leaked_to_the_next(self):
        async def usage_extract(unit):
            raw = await extract_fn(unit)
            if unit == UNITS[0]:  # only the first unit reports usage
                call_usage.set({"input_tokens": 10, "output_tokens": 20,
                                "total_tokens": 30, "reasoning_tokens": 15})
            return raw

        timing = TimingLog(self.path)
        timing.start()
        cq, rq = asyncio.Queue(maxsize=4), asyncio.Queue(maxsize=4)
        tasks = [
            asyncio.create_task(extract_worker(
                self.frontier, cq, rq, score_fn, usage_extract, 0.5, 0.5,
                extraction_units_fn=units_fn, poll_interval=0.01, timing=timing,
            )),
            asyncio.create_task(recrawl_stub(self.frontier, cq, CONTENT)),
        ]
        for _ in range(200):
            if rq.qsize() >= 1:
                break
            await asyncio.sleep(0.01)
        await cancel_all(tasks)
        await timing.close()

        calls = [e for e in read_events(self.path) if e["event"] == "extract_call"]
        self.assertEqual(calls[0]["usage"]["reasoning_tokens"], 15)
        self.assertIsNone(calls[1]["usage"])
        self.assertIsNone(calls[2]["usage"])

    def test_usage_from_message_reads_reasoning_tokens(self):
        from langchain_core.messages import AIMessage
        msg = AIMessage(content="x", usage_metadata={
            "input_tokens": 3, "output_tokens": 5, "total_tokens": 8,
            "output_token_details": {"reasoning": 4},
        })
        self.assertEqual(usage_from_message(msg), {
            "input_tokens": 3, "output_tokens": 5, "total_tokens": 8, "reasoning_tokens": 4,
        })
        self.assertIsNone(usage_from_message(AIMessage(content="x")))

    async def test_make_extract_fn_publishes_usage_only_when_asked(self):
        from unittest.mock import MagicMock

        from langchain_core.messages import AIMessage

        import main
        fake_llm = MagicMock()
        fake_llm.invoke.return_value = AIMessage(content="[]", usage_metadata={
            "input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
        })

        call_usage.set(None)
        self.assertEqual(await main._make_extract_fn(fake_llm)("text"), "[]")
        self.assertIsNone(call_usage.get())  # default: nothing published

        self.assertEqual(await main._make_extract_fn(fake_llm, record_usage=True)("text"), "[]")
        self.assertEqual(call_usage.get()["total_tokens"], 3)


if __name__ == "__main__":
    unittest.main()
