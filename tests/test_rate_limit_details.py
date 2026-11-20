"""429 details on RateLimitError (ROADMAP.md #51/#56, Brief 1 3c item A) --
offline. Real openai.RateLimitError objects built from fake httpx2
responses with Google- and OpenAI-shaped bodies, real requests.Response
objects for Jev, a fake LLM for main.py's extract_fn, and stub extract_fn
for extract_worker. No network, no real provider: a real 429 has never been
observed under concurrency, so these body shapes are the documented ones.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import httpx2
import openai
import requests

from config import RATE_LIMIT_MAX_BACKOFF_SECONDS
from crawl.frontier import Frontier
from crawl.pipeline import (
    RATE_LIMIT_DETAIL_MAX_CHARS,
    RateLimitError,
    extract_worker,
    rate_limit_details,
)
from crawl.timing import TimingLog

URL = "https://x.test/page"
GEMINI_BODY = [{"error": {
    "code": 429, "status": "RESOURCE_EXHAUSTED",
    "message": "Quota exceeded for metric generate_content_free_tier_requests, limit: 20",
    "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"},
    ]}}]


def sdk_error(status=429, body=None, headers=None):
    req = httpx2.Request("POST", "https://provider.test/v1/chat/completions")
    resp = httpx2.Response(status, request=req, headers=headers or {},
                           content=json.dumps(body).encode() if body is not None else b"")
    return openai.RateLimitError("rate limited", response=resp, body=body)


def read_events(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def requests_response(status=429, body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = json.dumps(body).encode() if body is not None else b""
    return r


class TestParsing(unittest.TestCase):
    def test_gemini_body_gives_retry_quota_and_message(self):
        d = rate_limit_details(sdk_error(body=GEMINI_BODY))
        self.assertEqual(d["status_code"], 429)
        self.assertEqual(d["retry_after"], 41.0)
        self.assertIn("GenerateRequestsPerDayPerProjectPerModel-FreeTier", d["quota_hint"])
        self.assertIn("generate_content_free_tier_requests", d["quota_hint"])
        self.assertTrue(d["detail"].startswith("Quota exceeded for metric"))

    def test_the_sdk_own_response_path_produces_a_parseable_body(self):
        """Guards against an SDK upgrade changing what lands in .body."""
        client = openai.OpenAI(api_key="test", base_url="https://provider.test/v1")
        if not hasattr(client, "_make_status_error_from_response"):
            self.skipTest("SDK internals changed; the public-constructor tests still apply")
        resp = httpx2.Response(429, request=httpx2.Request("POST", "https://provider.test/v1/x"), json=GEMINI_BODY)
        err = client._make_status_error_from_response(resp)
        self.assertIsInstance(err, openai.RateLimitError)
        self.assertEqual(rate_limit_details(err)["retry_after"], 41.0)

    def test_header_precedence_ms_then_seconds_then_body(self):
        both = {"retry-after-ms": "1500", "retry-after": "30"}
        self.assertEqual(rate_limit_details(sdk_error(body=GEMINI_BODY, headers=both))["retry_after"], 1.5)
        self.assertEqual(rate_limit_details(sdk_error(body=GEMINI_BODY, headers={"retry-after": "30"}))["retry_after"], 30.0)
        self.assertEqual(rate_limit_details(sdk_error(body=GEMINI_BODY))["retry_after"], 41.0)

    def test_retry_after_as_http_date(self):
        when = datetime.now(UTC) + timedelta(seconds=120)
        d = rate_limit_details(sdk_error(headers={"retry-after": format_datetime(when, usegmt=True)}))
        self.assertAlmostEqual(d["retry_after"], 120, delta=3)

    def test_unusable_retry_values_are_ignored(self):
        past = format_datetime(datetime.now(UTC) - timedelta(hours=1), usegmt=True)
        for value in ("0", "-5", "soon", "nan", past):
            self.assertIsNone(rate_limit_details(sdk_error(headers={"retry-after": value}))["retry_after"], value)

    def test_openai_shaped_body_and_no_body(self):
        d = rate_limit_details(sdk_error(body={"message": "Rate limit reached for requests", "type": "requests"}))
        self.assertEqual(d["detail"], "Rate limit reached for requests")
        self.assertIsNone(d["quota_hint"])
        self.assertIsNone(d["retry_after"])
        empty = rate_limit_details(sdk_error(body=None))
        self.assertEqual(empty, {"status_code": 429, "retry_after": None, "quota_hint": None, "detail": None})

    def test_long_message_is_capped_and_secrets_redacted(self):
        # Fake keys assembled at runtime so no key-shaped literal sits in the
        # source (the repo's pre-commit hook rightly blocks those).
        google_key = "AI" + "za" + "SyA" + "1234567890abcdefghijklmnopqrstu"
        openai_key = "s" + "k-" + "abcdefghijklmnopqrstuvwxyz012345"
        bearer = "abc.def-ghi_jkl"
        secret_body = {"message": (f"bad key {google_key} and {openai_key} "
                                   f"and Authorization: Bearer {bearer} " + "x" * 50_000)}
        d = rate_limit_details(sdk_error(body=secret_body))
        self.assertLessEqual(len(d["detail"]), RATE_LIMIT_DETAIL_MAX_CHARS)
        for leaked in (google_key, openai_key, bearer):
            self.assertNotIn(leaked, d["detail"])
        self.assertIn("[redacted]", d["detail"])

    def test_never_raises_on_odd_inputs(self):
        mock = MagicMock()  # what tests/test_decisions.py hands JevBackend
        mock.status_code = 429
        self.assertEqual(rate_limit_details(mock)["status_code"], 429)
        for odd in (None, "plain string", 42, ValueError("x"), {"error": "not a dict"}):
            rate_limit_details(odd)  # must not raise

    def test_from_provider_error_and_summary(self):
        e = RateLimitError.from_provider_error(sdk_error(body=GEMINI_BODY))
        self.assertEqual(e.retry_after, 41.0)
        self.assertIn("FreeTier", e.summary())
        self.assertEqual(RateLimitError().summary(), "")  # bare: unchanged meaning


class TestRaiseSites(unittest.IsolatedAsyncioTestCase):
    async def test_extract_fn_carries_details_and_keeps_the_cause(self):
        import main
        llm = MagicMock()
        cause = sdk_error(body=GEMINI_BODY)
        llm.invoke.side_effect = cause
        with self.assertRaises(RateLimitError) as ctx:
            await main._make_extract_fn(llm)("text")
        self.assertEqual(ctx.exception.retry_after, 41.0)
        self.assertIs(ctx.exception.__cause__, cause)

    def test_jev_429_reads_retry_after_header(self):
        from decisions.jev import JevBackend
        resp = requests_response(429, body={"error": {"message": "slow down"}}, headers={"Retry-After": "7"})
        with patch("decisions.jev.requests.post", return_value=resp), self.assertRaises(RateLimitError) as ctx:
            asyncio.run(JevBackend(api_key="sk-test").noul("s", "i"))
        self.assertEqual(ctx.exception.retry_after, 7.0)
        self.assertEqual(ctx.exception.detail, "slow down")


class TestWorkerUsesTheDetails(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.frontier = Frontier(":memory:", max_retries=3)
        await self.frontier.open()
        await self.frontier.seed([(URL, None)])

    async def asyncTearDown(self):
        await self.frontier.close()
        self.tmp.cleanup()

    async def _rate_limit_once(self, err, timing=None):
        cq, rq = asyncio.Queue(maxsize=4), asyncio.Queue(maxsize=4)
        raised = asyncio.Event()

        async def extract_fn(unit):
            raised.set()
            raise err

        async def score_fn(url, content):
            return 1.0

        async def recrawl():
            while True:
                row = await self.frontier.claim()
                if row is None:
                    await asyncio.sleep(0.01)
                    continue
                await self.frontier.put_content(cq, (row.url, "content"))

        tasks = [asyncio.create_task(extract_worker(self.frontier, cq, rq, score_fn, extract_fn, 0.5, 0.5,
                                                    poll_interval=0.01, timing=timing)),
                 asyncio.create_task(recrawl())]
        await asyncio.wait_for(raised.wait(), 5)
        for _ in range(100):
            cur = await self.frontier._conn.execute("SELECT status, not_before, last_error FROM frontier")
            row = await cur.fetchone()
            if row["status"] == "queued" and row["not_before"]:
                break
            await asyncio.sleep(0.01)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return row

    @staticmethod
    def _seconds_until(iso):
        return datetime.fromisoformat(iso).timestamp() - time.time()

    async def test_last_error_names_the_quota_and_backoff_uses_the_hint(self):
        timing = TimingLog(os.path.join(self.tmp.name, "t.jsonl"))
        timing.start()
        row = await self._rate_limit_once(RateLimitError.from_provider_error(sdk_error(body=GEMINI_BODY)), timing)
        await timing.close()
        self.assertTrue(row["last_error"].startswith("rate_limited (extraction): "))
        self.assertIn("FreeTier", row["last_error"])
        self.assertAlmostEqual(self._seconds_until(row["not_before"]), 41, delta=3)
        events = read_events(timing.path)
        (call,) = [e for e in events if e["event"] == "extract_call"]
        self.assertEqual(call["outcome"], "rate_limited")
        self.assertEqual(call["rate_limit"]["retry_after"], 41.0)

    async def test_backoff_is_capped(self):
        row = await self._rate_limit_once(RateLimitError(retry_after=72_000))  # "retry in 20 hours"
        self.assertAlmostEqual(self._seconds_until(row["not_before"]), RATE_LIMIT_MAX_BACKOFF_SECONDS, delta=3)

    async def test_bare_rate_limit_keeps_the_old_text_and_default(self):
        row = await self._rate_limit_once(RateLimitError())
        self.assertEqual(row["last_error"], "rate_limited (extraction)")
        self.assertAlmostEqual(self._seconds_until(row["not_before"]), 5, delta=2)


if __name__ == "__main__":
    unittest.main()
