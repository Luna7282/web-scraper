"""Items mode (headless contract v2 `items_mode: true`, foundry P1 Step 2):
fetch + clean only, one page record per page in items.jsonl, zero LLM calls.
No network: a fake site and fake robots fetch."""

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import headless
import main
from crawl.pipeline import FetchHTTPError

HOST = "127.0.0.1:9"
PAGES = {
    "/": ("# Home\n\nWelcome. See the guide.\n\n```python\nprint('hi')\n```\n", ["/guide", "/api"]),
    "/guide": ("# Guide\n\nStep one, then step two.\n", ["/"]),
    "/api": ("# API\n\nThe `run()` function.\n", []),
}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Crawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _NoProvider(AssertionError):
    pass


def _forbidden(*_a, **_k):
    raise _NoProvider("items mode must never construct a provider, embeddings, generators or verifiers")


class ItemsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fetched = []
        p = patch.object(config, "DEFAULT_POLITENESS_DELAY_SECONDS", 0.0)
        p.start()
        self.addCleanup(p.stop)
        self.fail_429 = None

    def site(self, crawler):
        async def fetch(url):
            self.fetched.append(url)
            path = url.split(HOST, 1)[1]
            if path == self.fail_429:
                raise FetchHTTPError.from_response(429, {"Retry-After": "30"})
            text, links = PAGES[path]
            return text, [f"http://{HOST}{link}" for link in links]
        return fetch

    def cfg(self, run_dir, **extra):
        settings = {"provider_preflight": True, "crawl_delay_seconds": 0.0, "stop_on_site_429": True,
                    **extra.pop("settings", {})}
        return {"contract_version": 2, "mode": "crawl", "run_dir": str(run_dir), "items_mode": True,
                "seeds": [[f"http://{HOST}/", None]], "host_prefix_pairs": [[HOST, "/"]],
                "provider": "ollama_cloud", "model": "m", "max_pages": 10, "max_depth": 3, "crawl_workers": 1,
                "extract_workers": 1, "intent": "", "settings": settings, **extra}

    def run_cfg(self, cfg, forbid=True):
        async def robots(url):
            return (200, "User-agent: *\nAllow: /\n")

        async def runner(c, api_key=None, context=None):
            return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=_Crawler,
                                             _fetch_fn_factory=self.site, _http_fetch=robots)

        patches = [patch.object(main, "_resolve_llm", _forbidden), patch.object(main, "build_generators", _forbidden),
                   patch.object(main, "build_verifiers", _forbidden),
                   patch("llm_factory.get_llm", _forbidden), patch("llm_factory.LocalOllamaEmbeddings", _forbidden),
                   patch.object(main, "provider_preflight", _forbidden),
                   patch.object(main, "embeddings_preflight", _forbidden)] if forbid else []
        for p in patches:
            p.start()
        try:
            err = io.StringIO()
            code = main.headless_v1_entry(cfg, run=runner, out=io.StringIO(), err=err, environ={})
        finally:
            for p in patches:
                p.stop()
        result = json.loads((Path(cfg["run_dir"]) / "result.json").read_text())
        return code, result, err.getvalue()

    def records(self, run_dir):
        return [json.loads(line) for line in (Path(run_dir) / "items.jsonl").read_text().splitlines()]


class TestItemsRun(ItemsCase):
    def test_one_record_per_page_and_nothing_llm_related(self):
        run_dir = Path(self.tmp.name) / "items"
        run_dir.mkdir()
        code, result, err = self.run_cfg(self.cfg(run_dir))
        self.assertEqual((code, result["outcome"]), (0, "ok"), err)
        recs = {r["url"]: r for r in self.records(run_dir)}
        self.assertEqual(sorted(recs), sorted(f"http://{HOST}{p}" for p in PAGES))
        home = recs[f"http://{HOST}/"]
        self.assertEqual(home["markdown"], PAGES["/"][0])
        self.assertEqual(home["content_sha256"], sha(PAGES["/"][0]))
        self.assertEqual((home["record_type"], home["schema_version"], home["title"], home["depth"],
                          home["parent_url"], home["chars"]), ("page", 1, "Home", 0, None, len(PAGES["/"][0])))
        self.assertEqual(recs[f"http://{HOST}/guide"]["parent_url"], f"http://{HOST}/")
        self.assertEqual(home["run_id"], result["run_id"])
        self.assertEqual(home["scraper_version"], result["scraper_version"])
        self.assertTrue(home["fetched_at"].endswith("+00:00"))
        # nothing LLM-related: no rows, no estimate, no cost / balance / verification, no provider preflight
        for name in ("canonical.jsonl", "estimate.json"):
            self.assertFalse((run_dir / name).exists(), name)
        for key in ("cost", "balance", "verification", "estimate", "dry_run"):
            self.assertNotIn(key, result, key)
        self.assertEqual(result["preflight"], {})
        self.assertEqual(result["items"]["pages_written"], 3)
        self.assertEqual(result["counts_by_status"], {"mapped": 3})
        (src,) = result["sources"]
        self.assertEqual((src["robots_txt"], src["licence"]), ("allowed", "none_found"))
        self.assertTrue((run_dir / "sources.json").exists())
        timings = (run_dir / "timings.jsonl")
        if timings.exists():
            kinds = {json.loads(line).get("event") for line in timings.read_text().splitlines()}
            self.assertFalse(kinds & {"extract_call", "quality_call", "breaker_open"}, kinds)

    def test_hashes_equal_the_dry_pass_page_fingerprints(self):
        items_dir, dry_dir = Path(self.tmp.name) / "items", Path(self.tmp.name) / "dry"
        items_dir.mkdir()
        dry_dir.mkdir()
        self.run_cfg(self.cfg(items_dir))
        dry = self.cfg(dry_dir)
        del dry["items_mode"]
        dry["dry_run"] = True
        dry["generators"] = [{"name": "qa", "version": 1}]
        code, result, err = self.run_cfg(dry, forbid=False)
        self.assertEqual(code, 0, err)
        fingerprints = result["estimate"]["page_fingerprints"]
        self.assertEqual({r["url"]: r["content_sha256"] for r in self.records(items_dir)},
                         {u: f["content_sha256"] for u, f in fingerprints.items()})

    def test_resume_never_writes_a_page_twice_and_moves_a_cut_tail_aside(self):
        run_dir = Path(self.tmp.name) / "resume"
        run_dir.mkdir()
        self.run_cfg(self.cfg(run_dir))
        self.assertEqual(len(self.records(run_dir)), 3)
        # a crash after the record was appended but before the page was marked done:
        # the page is claimable again, and a later write was cut short
        guide = f"http://{HOST}/guide"
        with sqlite3.connect(run_dir / "frontier.db") as db:
            db.execute("UPDATE frontier SET status = 'queued' WHERE url = ?", (guide,))
        with open(run_dir / "items.jsonl", "ab") as f:
            f.write(b'{"url": "http://cut')
        self.fetched.clear()
        code, result, err = self.run_cfg(self.cfg(run_dir, resume=True))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fetched, [guide])                       # re-fetched...
        self.assertEqual(result["items"]["already_written"], 1)       # ...but not written twice
        urls = [r["url"] for r in self.records(run_dir)]
        self.assertEqual(len(urls), 3)
        self.assertEqual(len(set(urls)), 3)
        tail = result["items"]["repaired_tail"]
        self.assertEqual(tail["moved_to"], f"items.tail.{result['run_id']}.bin")
        self.assertEqual((run_dir / tail["moved_to"]).read_bytes(), b'{"url": "http://cut')

    def test_the_first_site_429_stops_the_run(self):
        self.fail_429 = "/guide"
        run_dir = Path(self.tmp.name) / "429"
        run_dir.mkdir()
        code, result, _ = self.run_cfg(self.cfg(run_dir))
        self.assertEqual((code, result["outcome"]), (headless.EXIT_STOPPED_EARLY, "stopped_early"))
        self.assertIn("Site returned 429", result["fatal_errors"][0])
        self.assertEqual(self.fetched.count(f"http://{HOST}/guide"), 1)

    def test_a_write_failure_retries_the_page_instead_of_losing_it(self):
        run_dir = Path(self.tmp.name) / "wfail"
        run_dir.mkdir()
        calls = {"n": 0}
        real = main.append_page_atomically

        def flaky(path, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            real(path, data)
        with patch.object(main, "append_page_atomically", flaky):
            code, _result, err = self.run_cfg(self.cfg(run_dir))
        self.assertEqual(code, 0, err)
        self.assertEqual(len({r["url"] for r in self.records(run_dir)}), 3)


class TestItemsValidation(unittest.TestCase):
    def base(self, **extra):
        return {"contract_version": 2, "mode": "crawl", "run_dir": "/nonexistent-scraper-test-run-dir/x",
                "seeds": [["https://s.test/", None]], "host_prefix_pairs": [["s.test", "/"]], "items_mode": True,
                **extra}

    def test_refusals(self):
        for extra, msg in (({"dry_run": True}, "remove: dry_run"),
                           ({"generators": [{"name": "qa", "version": 1}]}, "remove: generators"),
                           ({"verifiers": []}, "remove: verifiers"),
                           ({"items_mode": "yes"}, "true or false"),
                           ({"mode": "map"}, "mode 'crawl'"),
                           ({"build_rag": True}, "build_rag must be off"),
                           ({"enable_quality_scoring": True}, "enable_quality_scoring must be off")):
            with self.subTest(extra=extra), self.assertRaisesRegex(headless.HeadlessConfigError, msg):
                headless.validate(self.base(**extra), environ={})

    def test_v1_refuses_it_as_an_unknown_key_and_v2_accepts_it(self):
        with self.assertRaisesRegex(headless.HeadlessConfigError, "unknown"):
            headless.validate({**self.base(), "contract_version": 1}, environ={})
        self.assertTrue(headless.validate(self.base(), environ={}).cfg["items_mode"])

    def test_items_jsonl_counts_as_run_state(self):
        self.assertIn("items.jsonl", headless.STATE_FILES)


if __name__ == "__main__":
    unittest.main()
