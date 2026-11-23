"""Brief 3B Step 6 run safety (contract v2 settings): a floor under per-host
request spacing (`crawl_delay_seconds`) and stopping the run at the first
site-side 429 (`stop_on_site_429`), from a page or from robots.txt. Also: in
v2, an error page crawl4ai reports as a success is an HTTP error. No network.
"""

import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import headless
import main
from crawl.pipeline import FetchHTTPError
from crawl.politeness import HostPoliteness

HOST = "127.0.0.1:9"


def v2cfg(**extra):
    return {"contract_version": 2, "mode": "crawl", "run_dir": "/nonexistent-scraper-test-run-dir/x",
            "seeds": [["https://s.test/", None]], "host_prefix_pairs": [["s.test", "/"]], **extra}


class TestSettings(unittest.TestCase):
    def test_defaults_types_and_v1(self):
        eff = headless.validate(v2cfg(), environ={}).effective
        self.assertEqual((eff["crawl_delay_seconds"]["value"], eff["stop_on_site_429"]["value"]), (None, False))
        eff = headless.validate(v2cfg(settings={"crawl_delay_seconds": 1, "stop_on_site_429": True}),
                                environ={}).effective
        self.assertEqual(eff["crawl_delay_seconds"]["value"], 1.0)
        self.assertIsInstance(eff["crawl_delay_seconds"]["value"], float)
        eff = headless.validate(v2cfg(), environ={"SCRAPER_CRAWL_DELAY_SECONDS": "1.5"}).effective
        self.assertEqual(eff["crawl_delay_seconds"], {"value": 1.5, "source": "env"})
        for bad, msg in (({"crawl_delay_seconds": -1}, ">= 0"), ({"crawl_delay_seconds": "1"}, "number"),
                         ({"crawl_delay_seconds": True}, "number"), ({"stop_on_site_429": 1}, "true or false")):
            with self.subTest(bad=bad), self.assertRaisesRegex(headless.HeadlessConfigError, msg):
                headless.validate(v2cfg(settings=bad), environ={})
        with self.assertRaisesRegex(headless.HeadlessConfigError, "unknown settings: crawl_delay_seconds"):
            headless.validate({**v2cfg(settings={"crawl_delay_seconds": 1}), "contract_version": 1}, environ={})


class TestPolitenessFloor(unittest.TestCase):
    def spacing(self, politeness, crawl_delay):
        async def go():
            starts = []
            for _ in range(2):
                async with politeness.hold("h", crawl_delay):
                    starts.append(time.monotonic())
            return starts[1] - starts[0]
        return asyncio.run(go())

    def test_the_floor_beats_a_smaller_site_crawl_delay_and_the_default(self):
        self.assertGreaterEqual(self.spacing(HostPoliteness(default_delay_seconds=0.0, min_delay_seconds=0.3), 0.0),
                                0.29)
        self.assertGreaterEqual(self.spacing(HostPoliteness(default_delay_seconds=0.0, min_delay_seconds=0.3), None),
                                0.29)
        self.assertLess(self.spacing(HostPoliteness(default_delay_seconds=0.0), 0.0), 0.1)  # no floor: as before
        # a larger site Crawl-delay still wins over the floor
        self.assertGreaterEqual(self.spacing(HostPoliteness(default_delay_seconds=0.0, min_delay_seconds=0.1), 0.4),
                                0.39)


class _Crawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestStopOnSite429(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fetched = []
        p = patch.object(config, "DEFAULT_POLITENESS_DELAY_SECONDS", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def site(self, crawler):
        async def fetch(url):
            self.fetched.append(url)
            path = url.split(HOST, 1)[1]
            if path == "/p1":
                raise FetchHTTPError.from_response(429, {"Retry-After": "30"})
            nxt = {"/": "/p1", "/p1": "/p2", "/p2": None}[path]
            return f"# Page {path}\n\nSome text about {path}.", [f"http://{HOST}{nxt}"] if nxt else []
        return fetch

    def run_v2(self, name, settings, robots_status=200):
        run_dir = Path(self.tmp.name) / name
        run_dir.mkdir()
        cfg = {"contract_version": 2, "mode": "crawl", "run_dir": str(run_dir),
               "seeds": [[f"http://{HOST}/", None]], "host_prefix_pairs": [[HOST, "/"]],
               "provider": "ollama_cloud", "model": "m", "max_pages": 5, "max_depth": 3, "crawl_workers": 1,
               "extract_workers": 1, "intent": "",
               "settings": {"llm_breaker_threshold": 0, "provider_preflight": False, **settings}}

        async def robots(url):
            return (robots_status, "User-agent: *\nAllow: /\n" if robots_status == 200 else None)

        async def extract(unit):
            return '[{"instruction": "What is it about?", "response": "It is about a page of text."}]'

        async def runner(c, api_key=None, context=None):
            return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=_Crawler,
                                             _fetch_fn_factory=self.site, _extract_fn=extract, _http_fetch=robots,
                                             _llm=object())

        code = main.headless_v1_entry(cfg, run=runner, out=io.StringIO(), err=io.StringIO(),
                                      environ={"OLLAMA_API_KEY": "dummy"})
        return code, json.loads((run_dir / "result.json").read_text())

    def test_the_first_page_429_stops_the_run(self):
        code, result = self.run_v2("stop", {"stop_on_site_429": True})
        self.assertEqual((code, result["outcome"]), (headless.EXIT_STOPPED_EARLY, "stopped_early"))
        self.assertIn("Site returned 429", result["fatal_errors"][0])
        self.assertNotIn(f"http://{HOST}/p2", self.fetched)   # nothing fetched after it
        self.assertEqual(self.fetched.count(f"http://{HOST}/p1"), 1)  # never retried

    def test_without_the_setting_a_429_is_retried_as_before(self):
        code, result = self.run_v2("retry", {"site_retry_backoff": False})
        self.assertEqual(code, 0)
        self.assertGreater(self.fetched.count(f"http://{HOST}/p1"), 1)
        self.assertEqual(result["fatal_errors"], [])

    def test_a_robots_txt_429_stops_the_run_before_any_page(self):
        code, result = self.run_v2("robots", {"stop_on_site_429": True}, robots_status=429)
        self.assertEqual(code, headless.EXIT_STOPPED_EARLY)
        self.assertIn("robots.txt", result["fatal_errors"][0])
        self.assertEqual(result["sources"][0]["details"]["robots_status"], 429)
        self.assertLessEqual(len(self.fetched), 1)  # at most the page whose robots check triggered it


class TestStrictStatusInV2(unittest.TestCase):
    def fetch(self, strict, status):
        result = SimpleNamespace(success=True, status_code=status, redirected_status_code=None,
                                 response_headers={"retry-after": "7"}, html="<p>Too many requests</p>",
                                 markdown="Too many requests", links={"internal": [], "external": []},
                                 error_message=None)

        class Crawler:
            async def arun(self, url, config):
                return result
        return asyncio.run(main._make_fetch_fn(Crawler(), strict_status=strict)("http://x.test/"))

    def test_an_error_page_reported_as_success_is_an_http_error_in_v2_only(self):
        with self.assertRaises(FetchHTTPError) as ctx:
            self.fetch(True, 429)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(self.fetch(False, 429)[0], "Too many requests")  # legacy / v1: unchanged
        self.assertEqual(self.fetch(True, 200)[0], "Too many requests")


if __name__ == "__main__":
    unittest.main()
