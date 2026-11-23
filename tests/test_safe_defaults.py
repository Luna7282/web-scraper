"""Brief 3A Step 4: safe defaults in v1 headless mode (item 18).

- v1 defaults: failed-page budget on, LLM breaker at 5, site 429/503 backoff
  on, LLM 5xx releases capped at 5 per page. config.py (legacy) unchanged.
- Site 429/503: re-queued with the site's Retry-After, else 5s doubling.
- LLM 500/502/503/504: released with a backoff, no retry used, at most 5
  times per page; then the page fails with a clear reason (persistent 503).
- The spiral regression: persistent 429s with the v1 defaults trip the
  breaker and stop the run, instead of burning every page's retries.

Fake provider (an extract_fn), fake site (fetch_fn) and fake crawler. No LLM,
no network. In-process runs shrink the backoffs/cooldowns via patch, and
ConfigGuard-style checks confirm config is restored.
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import config
import headless
import main
from crawl import pipeline
from crawl.frontier import Frontier
from crawl.pipeline import (
    FetchHTTPError,
    ProviderUnavailableError,
    RateLimitError,
    crawl_worker,
    doubling_backoff,
)

REPO = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1:9"  # a closed local port: robots.txt is refused locally, never a real request
STEP4_ATTRS = ("MAX_PAGES_COUNTS_FAILED", "LLM_BREAKER_THRESHOLD", "SITE_RETRY_BACKOFF",
               "LLM_UNAVAILABLE_MAX_RELEASES", "LLM_SDK_MAX_RETRIES")


SEEDS = {"seeds": [["http://site.test/", None]], "host_prefix_pairs": [["site.test", "/"]]}


def _seconds_until(iso: str) -> float:
    return (datetime.fromisoformat(iso) - datetime.now(UTC)).total_seconds()


class TestDefaults(unittest.TestCase):
    def test_v1_defaults_are_on_and_the_environment_can_still_turn_them_off(self):
        hr = headless.validate({"contract_version": 1, "mode": "crawl", "run_dir": "/x", **SEEDS}, environ={})
        got = {k: hr.effective[k] for k in ("max_pages_counts_failed", "llm_breaker_threshold",
                                            "site_retry_backoff", "llm_unavailable_max_releases")}
        self.assertEqual(got, {"max_pages_counts_failed": {"value": True, "source": "default"},
                               "llm_breaker_threshold": {"value": 5, "source": "default"},
                               "site_retry_backoff": {"value": True, "source": "default"},
                               "llm_unavailable_max_releases": {"value": 5, "source": "default"}})
        off = headless.validate({"contract_version": 1, "mode": "crawl", "run_dir": "/x", **SEEDS,
                                 "settings": {"llm_breaker_threshold": 0}},
                                environ={"SCRAPER_SITE_RETRY_BACKOFF": "false", "MAX_PAGES_COUNTS_FAILED": "false"})
        self.assertEqual(off.effective["llm_breaker_threshold"], {"value": 0, "source": "config"})
        self.assertEqual(off.effective["site_retry_backoff"], {"value": False, "source": "env"})
        self.assertEqual(off.effective["max_pages_counts_failed"], {"value": False, "source": "env"})

    def test_config_py_defaults_are_unchanged_for_legacy(self):
        """Fresh interpreter, none of the variables set: today's values."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("MAX_PAGES_COUNTS_FAILED", "LLM_BREAKER_THRESHOLD", "SCRAPER_SITE_RETRY_BACKOFF",
                            "SCRAPER_LLM_UNAVAILABLE_MAX_RELEASES", "LLM_SDK_MAX_RETRIES")}
        env["SCRAPER_LOAD_DOTENV"] = "0"
        code = f"import config; print([getattr(config, a) for a in {STEP4_ATTRS!r}])"
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True,
                             check=True).stdout.strip()
        self.assertEqual(out, "[False, 0, False, 0, 2]")

    def test_applied_settings_turn_them_on_for_the_run_and_restore_config(self):
        before = {a: getattr(config, a) for a in STEP4_ATTRS}
        hr = headless.validate({"contract_version": 1, "mode": "crawl", "run_dir": "/x", **SEEDS}, environ={})
        with headless.applied_settings(hr.effective):
            self.assertEqual((config.MAX_PAGES_COUNTS_FAILED, config.LLM_BREAKER_THRESHOLD,
                              config.SITE_RETRY_BACKOFF, config.LLM_UNAVAILABLE_MAX_RELEASES), (True, 5, True, 5))
            self.assertEqual(config.effective_llm_sdk_max_retries(), 0)  # breaker on forces SDK retries off
        self.assertEqual({a: getattr(config, a) for a in STEP4_ATTRS}, before)


class TestBackoffHelpers(unittest.TestCase):
    def test_doubling_backoff_prefers_the_hint_and_is_capped(self):
        self.assertEqual([doubling_backoff(n, None) for n in range(4)], [5.0, 10.0, 20.0, 40.0])
        self.assertEqual(doubling_backoff(0, 42.0), 42.0)
        self.assertEqual(doubling_backoff(20, None), config.RATE_LIMIT_MAX_BACKOFF_SECONDS)
        self.assertEqual(doubling_backoff(0, 99999.0), config.RATE_LIMIT_MAX_BACKOFF_SECONDS)

    def test_fetch_http_error_reads_retry_after_from_any_header_case(self):
        self.assertEqual(FetchHTTPError.from_response(429, {"Retry-After": "7"}).retry_after, 7.0)
        self.assertEqual(FetchHTTPError.from_response(503, {"retry-after": "12"}).retry_after, 12.0)
        self.assertIsNone(FetchHTTPError.from_response(429, {}).retry_after)
        self.assertIsNone(FetchHTTPError.from_response(429, None).retry_after)
        self.assertIsNone(FetchHTTPError.from_response(429, {"Retry-After": "soon"}).retry_after)


class _StatusError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class TestExtractFnMapping(unittest.TestCase):
    def _call(self, status, max_releases):
        class Llm:
            def invoke(self, messages):
                raise _StatusError(status)
        fn = main._make_extract_fn(Llm())
        with patch.object(config, "LLM_UNAVAILABLE_MAX_RELEASES", max_releases):
            try:
                asyncio.run(fn("text"))
            except Exception as e:  # noqa: BLE001 -- the type is what is under test
                return e

    def test_5xx_becomes_provider_unavailable_only_when_enabled(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                e = self._call(status, 5)
                self.assertIsInstance(e, ProviderUnavailableError)
                self.assertEqual(e.status_code, status)
                self.assertIsInstance(self._call(status, 0), _StatusError)  # legacy: unchanged

    def test_429_and_permanent_errors_are_untouched(self):
        self.assertIsInstance(self._call(429, 5), RateLimitError)
        for status in (400, 401, 404):
            self.assertIsInstance(self._call(status, 5), _StatusError)


class TestSiteBackoff(unittest.IsolatedAsyncioTestCase):
    async def _one_refusal(self, error, site_backoff, retry_count=0):
        f = Frontier(":memory:", max_retries=10)
        await f.open()
        await f.recover_crashed()
        await f.seed([("http://site.test/a", None)])
        if retry_count:
            await f._conn.execute("UPDATE frontier SET retry_count = ?", (retry_count,))
            await f._conn.commit()

        async def fetch(url):
            raise error

        task = asyncio.create_task(crawl_worker(f, fetch, asyncio.Queue(), lambda u: True, poll_interval=0.01,
                                                site_backoff=site_backoff))
        for _ in range(200):
            cur = await f._conn.execute("SELECT status, retry_count, last_error, not_before FROM frontier")
            row = tuple(await cur.fetchone())
            if row[1] > retry_count:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await f.close()
        return row

    async def test_429_and_503_back_off_when_enabled(self):
        status, retries, err, not_before = await self._one_refusal(FetchHTTPError(429), True)
        self.assertEqual((status, retries, err), ("queued", 1, "http_429 (backoff 5s)"))
        self.assertAlmostEqual(_seconds_until(not_before), 5, delta=1)
        _, _, err, not_before = await self._one_refusal(FetchHTTPError(503), True, retry_count=2)
        self.assertEqual(err, "http_503 (backoff 20s)")  # third attempt: 5 -> 10 -> 20
        self.assertAlmostEqual(_seconds_until(not_before), 20, delta=1)

    async def test_the_sites_retry_after_wins(self):
        _, _, err, not_before = await self._one_refusal(FetchHTTPError(429, retry_after=42.0), True)
        self.assertEqual(err, "http_429 (backoff 42s)")
        self.assertAlmostEqual(_seconds_until(not_before), 42, delta=1)

    async def test_legacy_and_other_statuses_retry_at_once(self):
        # No backoff: the page is re-fetched at once, often several times
        # before this test looks (today's behaviour, ROADMAP #52).
        for error, backoff_on, reason in ((FetchHTTPError(429), False, "http_429"),
                                          (FetchHTTPError(404), True, "http_404")):
            status, retries, err, not_before = await self._one_refusal(error, backoff_on)
            self.assertIn(status, ("queued", "in_progress"))  # caught mid re-claim
            self.assertEqual((err, not_before), (reason, None))
            self.assertGreaterEqual(retries, 1)


class TestReleaseCap(unittest.IsolatedAsyncioTestCase):
    async def test_releases_keep_retry_count_then_the_page_fails_with_a_reason(self):
        f = Frontier(":memory:", max_retries=3)
        await f.open()
        await f.recover_crashed()
        await f.seed([("http://site.test/a", None)])
        backoffs = []
        for n in range(1, 7):
            await f._conn.execute("UPDATE frontier SET not_before = NULL")
            await f._conn.commit()
            self.assertIsNotNone(await f.claim())
            failed = await f.release_llm_unavailable("http://site.test/a", "llm_unavailable (extraction): HTTP 503",
                                                     lambda k: backoffs.append(k) or 5.0, 5)
            self.assertEqual(failed, n == 6)
        cur = await f._conn.execute("SELECT status, retry_count, last_error FROM frontier")
        status, retries, err = tuple(await cur.fetchone())
        self.assertEqual((status, retries), ("failed", 0))
        self.assertIn("gave up after 5 releases", err)
        self.assertEqual(backoffs, [1, 2, 3, 4, 5])
        self.assertEqual(f.in_flight, 0)
        await f.close()


# ---------------------------------------------------------------------------
# whole v1 runs, in process, with a fake site and a fake provider
# ---------------------------------------------------------------------------
class _Crawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _site(pages):
    def factory(crawler):
        async def fetch(url):
            i = int(url.rsplit("/p", 1)[1])
            links = [f"http://{HOST}/p{j}" for j in (i + 1,) if j < pages]
            return f"# Page {i}\n\nText of page {i}.", links
        return fetch
    return factory


class TestV1Runs(unittest.TestCase):
    """A v1 entry run with the v1 defaults; only timings are shrunk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()
        self.calls = 0
        self.before = {a: getattr(config, a) for a in STEP4_ATTRS}
        fast = [patch.object(pipeline, "DEFAULT_RATE_LIMIT_BACKOFF_SECONDS", 0.01),
                patch.object(config, "DEFAULT_POLITENESS_DELAY_SECONDS", 0.0),
                patch.object(config, "LLM_BREAKER_BASE_COOLDOWN_SECONDS", 0.05),
                patch.object(config, "LLM_BREAKER_MAX_COOLDOWN_SECONDS", 0.2)]
        for p in fast:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()
        self.assertEqual({a: getattr(config, a) for a in STEP4_ATTRS}, self.before)  # config restored

    def run_v1(self, extract, pages=3, settings=None):
        cfg = {"contract_version": 1, "mode": "crawl", "run_dir": str(self.run_dir),
               "seeds": [[f"http://{HOST}/p0", None]], "host_prefix_pairs": [[HOST, "/"]],
               "provider": "gemini", "model": "fake", "max_pages": pages, "crawl_workers": 1,
               "extract_workers": 1, "intent": "", "settings": settings or {}}

        async def run(c, api_key=None, context=None):
            return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=_Crawler,
                                             _fetch_fn_factory=_site(pages), _extract_fn=extract)

        out, err = _Sink(), _Sink()
        code = main.headless_v1_entry(cfg, run=run, out=out, err=err, environ={})
        return code, headless_result(self.run_dir)

    def rows(self):
        con = sqlite3.connect(self.run_dir / "frontier.db")
        try:
            return con.execute("SELECT url, status, retry_count, last_error FROM frontier ORDER BY url").fetchall()
        finally:
            con.close()

    def test_persistent_503_releases_five_times_per_page_then_fails_it(self):
        async def always_503(unit):
            self.calls += 1
            raise ProviderUnavailableError(status_code=503, detail="high demand")

        code, result = self.run_v1(always_503)
        self.assertEqual((code, result["outcome"]), (0, "ok"))  # pages failed; the run itself ended normally
        self.assertEqual(result["counts_by_status"], {"failed": 3})
        self.assertEqual(self.calls, 3 * 6)  # 5 releases + the final attempt, per page
        for _url, status, retries, err in self.rows():
            self.assertEqual((status, retries), ("failed", 0))  # no retry budget used
            self.assertIn("llm_unavailable (extraction): HTTP 503", err)
            self.assertIn("gave up after 5 releases", err)

    def test_a_503_spike_that_clears_loses_nothing(self):
        async def flaky(unit):
            self.calls += 1
            if self.calls <= 4:
                raise ProviderUnavailableError(status_code=503)
            return '[{"instruction": "Q?", "response": "An answer long enough."}]'

        code, result = self.run_v1(flaky)
        self.assertEqual((code, result["counts_by_status"]), (0, {"done": 3}))
        self.assertTrue(all(retries == 0 for _u, _s, retries, _e in self.rows()))

    def test_spiral_regression_persistent_429_trips_the_breaker_and_keeps_pages_resumable(self):
        """ROADMAP #54 / LESSONS #81: without the breaker every 429 released a
        page and the next page 429'd too, until every page's retries were
        gone. With the v1 default (threshold 5) the run stops instead."""
        async def always_429(unit):
            self.calls += 1
            raise RateLimitError()

        code, result = self.run_v1(always_429, pages=6)
        self.assertEqual((code, result["outcome"]), (1, "stopped_early"))
        self.assertTrue(any("circuit breaker stopped the run" in m for m in result["fatal_errors"]))
        self.assertNotIn("failed", result["counts_by_status"])  # nothing burned to failed
        self.assertTrue(all(retries < config.MAX_RETRIES for _u, _s, retries, _e in self.rows()))

    def test_spiral_without_the_breaker_is_what_v1_now_prevents(self):
        """The same fake provider with the breaker turned off: pages end
        failed from rate limiting alone (the behaviour the default removes)."""
        async def always_429(unit):
            raise RateLimitError()

        code, result = self.run_v1(always_429, pages=2, settings={"llm_breaker_threshold": 0})
        self.assertEqual(code, 0)
        self.assertEqual(result["counts_by_status"], {"failed": 2})
        self.assertTrue(all(retries == config.MAX_RETRIES for _u, _s, retries, _e in self.rows()))


class _Sink:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s

    def flush(self):
        pass


def headless_result(run_dir):
    import json
    return json.loads((Path(run_dir) / "result.json").read_text())


if __name__ == "__main__":
    unittest.main()
