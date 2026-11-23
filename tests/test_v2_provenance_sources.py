"""Brief 3B Step 1: provenance (item 14), source records (item 16) and the
trailing-slash / robots fix (item 17), all contract v2 only.

Offline: recorded robots.txt fixtures, synthetic HTML, fake crawler / fetch /
extractor, in-memory frontiers. No network, no LLM.
"""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import headless
import main
from crawl.frontier import ROBOTS_DISALLOWED_ERROR, Frontier
from crawl.pipeline import crawl_worker
from crawl.provenance import call_served_model
from crawl.robots_cache import RobotsCache
from crawl.scope import fetch_form, normalize_url
from crawl.sources import (
    NOTHING_FOUND,
    ROBOTS_RESULTS,
    SourceRecorder,
    ai_agent_rules,
    licence_evidence,
    page_noai_tags,
    parse_content_signals,
)
from llm_factory import ServedModelChatOpenAI
from storage.canonical import (
    CANONICAL_FIELDS,
    PROVENANCE_FIELDS,
    build_canonical_record,
    build_canonical_record_v2,
)

FIXTURES = Path(__file__).parent / "fixtures" / "robots"
HOST = "127.0.0.1:9"  # a closed local port: nothing is ever requested from it


def chat_response(model="served-model-x"):
    data = {"id": "c1", "object": "chat.completion", "created": 0,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "[]"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
    if model is not None:
        data["model"] = model
    return data


# ---------------------------------------------------------------------------
# item 14: provenance
# ---------------------------------------------------------------------------
class TestServedModel(unittest.TestCase):
    def setUp(self):
        self.llm = ServedModelChatOpenAI(model="requested-model", api_key="dummy", base_url="http://127.0.0.1:9/v1")

    def served(self, response):
        return self.llm._create_chat_result(response).generations[0].message.response_metadata

    def test_the_raw_response_model_is_recorded(self):
        self.assertEqual(self.served(chat_response("provider-served-v7"))["served_model"], "provider-served-v7")

    def test_absent_means_none_not_the_requested_model(self):
        """langchain fills model_name with the REQUESTED model when the field is
        missing; served_model must say None instead."""
        for response in (chat_response(None), chat_response(""), chat_response("  ")):
            self.assertIsNone(self.served(response)["served_model"])
        self.assertEqual(self.llm._create_chat_result(chat_response(None)).llm_output["model_name"],
                         "requested-model")  # the fallback this subclass exists to see past

    def test_it_survives_a_real_invoke(self):
        """Through invoke() and langchain's own metadata merging, over a mock
        HTTP transport (no network)."""
        import httpx
        from langchain_core.messages import HumanMessage
        for model, expected in (("provider-served-v7", "provider-served-v7"), (None, None)):
            body = chat_response(model)
            transport = httpx.MockTransport(lambda request, body=body: httpx.Response(200, json=body))
            llm = ServedModelChatOpenAI(model="requested-model", api_key="dummy", base_url="http://127.0.0.1:9/v1",
                                        http_client=httpx.Client(transport=transport), max_retries=0)
            message = llm.invoke([HumanMessage(content="hi")])
            self.assertEqual(message.response_metadata["served_model"], expected)

    def test_get_llm_builds_the_subclass(self):
        from llm_factory import get_llm
        llm, _ = get_llm("ollama_cloud", "m", api_key="dummy")
        self.assertIsInstance(llm, ServedModelChatOpenAI)


class TestCanonicalV2(unittest.TestCase):
    FIELDS: ClassVar[dict] = {"question": "Q?", "answer": "An answer.", "source_chunk": "chunk", "chunk_index": 0,
              "source_url": "https://x.test/a", "section": "a", "page_title": "A", "generation_model": "m",
              "extraction_strategy": "per_chunk", "timestamp": "t", "crawl_date": "d", "license_signal": None,
              "quality_score": None, "quality_decision_method": None, "scraper_version": "abc1234"}
    PROV: ClassVar[dict] = {"generator": {"name": "qa", "version": 1}, "provider": "ollama_cloud", "model": "m",
            "pinned_version": "m@2026-11", "endpoint": "https://ollama.com/v1", "scraper_version": "abc1234",
            "run_id": "r1", "prompt_sha256": "0" * 64}

    def test_v2_row_shape(self):
        row = build_canonical_record_v2(self.FIELDS, record_type="qa", provenance=self.PROV, served_model=None)
        self.assertEqual(list(row)[:2], ["schema_version", "record_type"])
        self.assertEqual((row["schema_version"], row["record_type"]), (2, "qa"))
        self.assertEqual(tuple(row["provenance"]), PROVENANCE_FIELDS)
        self.assertIsNone(row["provenance"]["served_model"])
        self.assertEqual(row["provenance"]["scraper_version"], "abc1234")
        self.assertNotIn("scraper_version", row)  # moved into provenance
        for k in CANONICAL_FIELDS:
            self.assertEqual(row[k], self.FIELDS[k])  # qa rows keep question/answer at the top: exporters work

    def test_incomplete_provenance_is_refused(self):
        with self.assertRaises(ValueError):
            build_canonical_record_v2(self.FIELDS, record_type="qa", provenance={"provider": "x"}, served_model=None)

    def test_v1_rows_are_unchanged(self):
        v1 = build_canonical_record(**self.FIELDS)
        self.assertNotIn("schema_version", v1)
        self.assertEqual(list(v1), [*CANONICAL_FIELDS, "scraper_version"])


class TestContractV2Keys(unittest.TestCase):
    def cfg(self, **kw):
        return {"mode": "crawl", "run_dir": "/nonexistent-scraper-test-run-dir/x",
                "seeds": [["https://x.test/", None]], "host_prefix_pairs": [["x.test", "/"]], **kw}

    def test_model_pinned_version_is_v2_only(self):
        hr = headless.validate(self.cfg(contract_version=2, model_pinned_version="m@2026-11-01"), {})
        self.assertEqual(hr.contract_version, 2)
        with self.assertRaises(headless.HeadlessConfigError) as ctx:
            headless.validate(self.cfg(contract_version=1, model_pinned_version="m"), {})
        self.assertIn("unknown config keys: model_pinned_version", str(ctx.exception))
        with self.assertRaises(headless.HeadlessConfigError):
            headless.validate(self.cfg(contract_version=2, model_pinned_version=" "), {})


# ---------------------------------------------------------------------------
# item 16: source records
# ---------------------------------------------------------------------------
class TestSignals(unittest.TestCase):
    def test_content_signal_lines_not_comments(self):
        cloudflare = (FIXTURES / "blog_cloudflare_robots.txt").read_text()
        self.assertEqual(parse_content_signals(cloudflare), ["ai-train=yes, search=yes, ai-input=yes"])
        fastapi = (FIXTURES / "fastapi_tiangolo_robots.txt").read_text()
        self.assertIn("content-signal", fastapi.lower())       # mentioned in its comments...
        self.assertEqual(parse_content_signals(fastapi), [])   # ...but no directive

    def test_ai_agent_rules_only_for_explicitly_named_agents(self):
        text = ("User-agent: *\nDisallow: /private/\n\n"
                "User-agent: GPTBot   # OpenAI\nDisallow: /\n\n"
                "User-agent: CCBot\nAllow: /\n")
        self.assertEqual(ai_agent_rules(text), ["GPTBot: root disallowed", "CCBot: root allowed"])
        self.assertEqual(ai_agent_rules("User-agent: *\nDisallow: /\n"), [])  # only '*': not an AI statement

    def test_noai_tags_in_header_and_meta(self):
        self.assertEqual(page_noai_tags({"X-Robots-Tag": "noai, noimageai"}, ""), ["x-robots-tag: noai, noimageai"])
        html = '<html><head><meta name="robots" content="noai"></head><body>x</body></html>'
        self.assertEqual(len(page_noai_tags({}, html)), 1)
        self.assertEqual(page_noai_tags({"x-robots-tag": "noindex"}, "<p>noai in text</p>"), [])

    def test_licence_evidence_is_snippets_with_their_page(self):
        html = ("<html><head><link rel=\"license\" href=\"https://x.test/LICENSE\"></head><body>" + "x" * 500
                + "<footer>Code licensed under the MIT License. Text under "
                "<a href=\"https://creativecommons.org/licenses/by-sa/4.0/\">CC BY-SA 4.0</a>.</footer></body></html>")
        ev = licence_evidence(html, "https://x.test/page")
        kinds = {e["kind"] for e in ev}
        self.assertTrue({"rel=license link", "MIT License", "Creative Commons licence link"} <= kinds)
        self.assertTrue(all(e["url"] == "https://x.test/page" and 0 < len(e["snippet"]) <= 160 for e in ev))
        self.assertEqual(licence_evidence("<p>No licence words here.</p>", "u"), [])


class TestSourceRecords(unittest.TestCase):
    def build(self, status, text, counts=None, pages=()):
        rec = SourceRecorder(clock=lambda: 1795178914.0)
        rec.record_robots("https://site.test/robots.txt", status, text)
        for url, headers, html in pages:
            rec.observe_page(url, headers, html)
        return rec.build({"site.test": counts} if counts else {})

    def test_every_field_present_and_in_foundrys_vocabulary(self):
        cases = [((404, None, None), "no_robots_txt"), ((None, None, None), "fetch_failed"),
                 ((500, "x", None), "fetch_failed"), ((200, "User-agent: *\n", None), "allowed"),
                 ((200, "", {"urls": 2, "seeds": 1, "seeds_disallowed": 0}), "partially_disallowed"),
                 ((200, "", {"urls": 1, "seeds": 1, "seeds_disallowed": 1}), "disallowed")]
        for (status, text, counts), expected in cases:
            with self.subTest(expected=expected):
                (record,) = self.build(status, text, counts)
                self.assertEqual(record["robots_txt"], expected)
                self.assertIn(record["robots_txt"], ROBOTS_RESULTS)
                for f in ("site", "robots_txt", "ai_training_signal", "licence", "checked_at"):
                    self.assertIsInstance(record[f], str)
                    self.assertTrue(record[f].strip(), f)
                self.assertEqual(record["checked_at"], "2026-11-20T12:48:34Z")  # the injected clock, UTC

    def test_nothing_found_is_explicit(self):
        (record,) = self.build(200, "User-agent: *\nAllow: /\n")
        self.assertEqual((record["ai_training_signal"], record["licence"]), (NOTHING_FOUND, NOTHING_FOUND))

    def test_signals_and_licence_are_reported_as_evidence(self):
        robots = (FIXTURES / "blog_cloudflare_robots.txt").read_text() + "\nUser-agent: GPTBot\nDisallow: /\n"
        html = "<footer>Licensed under the Apache License, Version 2.0</footer>"
        (record,) = self.build(200, robots, pages=[("https://site.test/a", {"X-Robots-Tag": "noai"}, html)])
        self.assertIn("content-signal: ai-train=yes", record["ai_training_signal"])
        self.assertIn("GPTBot: root disallowed", record["ai_training_signal"])
        self.assertIn("noai tag on 1 page(s)", record["ai_training_signal"])
        self.assertEqual(record["licence"], "evidence only (not a legal determination): Apache License 2.0")
        self.assertEqual(record["details"]["licence_evidence"][0]["url"], "https://site.test/a")

    def test_a_site_never_contacted_has_no_record(self):
        rec = SourceRecorder()
        rec.observe_page("https://other.test/a", {}, "")
        self.assertEqual(rec.build({}), [])


# ---------------------------------------------------------------------------
# item 17: request form vs identity (ROADMAP #53)
# ---------------------------------------------------------------------------
class TestFetchForm(unittest.TestCase):
    def test_identity_drops_the_slash_the_request_keeps_it(self):
        base = "https://docs.manim.community/en/stable/installation.html"
        self.assertEqual(normalize_url("/en/stable/", base), "https://docs.manim.community/en/stable")
        self.assertEqual(fetch_form("/en/stable/", base), "https://docs.manim.community/en/stable/")
        self.assertEqual(fetch_form("/en/stable", base), "https://docs.manim.community/en/stable")
        self.assertEqual(fetch_form("/a/?b=2&a=1#x", base), "https://docs.manim.community/a/?a=1&b=2")
        self.assertEqual(fetch_form("/", base), "https://docs.manim.community/")
        self.assertIsNone(fetch_form("mailto:x@y.z", base))


class TestSlashRobotsFix(unittest.IsolatedAsyncioTestCase):
    """docs.manim.community's real robots.txt: Disallow / + Allow /en/stable/.
    A page links to the section index /en/stable/. Legacy/v1: the row
    /en/stable is checked as-is and refused. v2: checked and fetched as
    /en/stable/, which the file allows."""

    SEED = "https://docs.manim.community/en/stable/installation.html"
    INDEX = "https://docs.manim.community/en/stable"

    async def crawl(self, use_fetch_urls):
        robots = (FIXTURES / "docs_manim_community_robots.txt").read_text()
        requested = []

        async def fetch_text(url):
            return robots if url.endswith("/robots.txt") else None

        async def fetch(url):
            requested.append(url)
            return "# page", ["/en/stable/"] if url == self.SEED else []

        f = Frontier(":memory:")
        await f.open()
        await f.recover_crashed()
        await f.seed([(self.SEED, None)])
        if use_fetch_urls:
            await f.enable_fetch_urls()
        queue = asyncio.Queue()
        task = asyncio.create_task(crawl_worker(f, fetch, queue, lambda u: True, RobotsCache(fetch_text),
                                                poll_interval=0.01, use_fetch_urls=use_fetch_urls))
        for _ in range(300):
            cur = await f._conn.execute("SELECT status, last_error FROM frontier WHERE url = ?", (self.INDEX,))
            row = await cur.fetchone()
            if row is not None and (row[0] == "failed" or len(requested) >= 2):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await f.close()
        return tuple(row), requested

    async def test_legacy_refuses_the_index_page(self):
        (status, error), requested = await self.crawl(use_fetch_urls=False)
        self.assertEqual((status, error), ("failed", ROBOTS_DISALLOWED_ERROR))
        self.assertEqual(requested, [self.SEED])

    async def test_v2_requests_it_with_its_slash(self):
        (_status, error), requested = await self.crawl(use_fetch_urls=True)
        self.assertNotEqual(error, ROBOTS_DISALLOWED_ERROR)
        self.assertEqual(requested, [self.SEED, self.INDEX + "/"])  # the form robots.txt allows


# ---------------------------------------------------------------------------
# a whole v2 run, in process
# ---------------------------------------------------------------------------
class _Crawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _site(crawler):
    async def fetch(url):
        i = int(url.rstrip("/").rsplit("/p", 1)[1])
        return f"# Page {i}\n\nText of page {i}.", [f"http://{HOST}/p{i + 1}/"] if i < 2 else []
    return fetch


class TestV2Run(unittest.TestCase):
    def run_entry(self, version):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name) / "run"
        run_dir.mkdir()
        cfg = {"contract_version": version, "mode": "crawl", "run_dir": str(run_dir),
               "seeds": [[f"http://{HOST}/p0", None]], "host_prefix_pairs": [[HOST, "/"]],
               "provider": "ollama_cloud", "model": "requested-model", "max_pages": 3, "crawl_workers": 1,
               "extract_workers": 1, "intent": "", "settings": {"llm_breaker_threshold": 0}}
        if version == 2:
            cfg["model_pinned_version"] = "requested-model@2026-11-01"
            cfg["settings"]["provider_preflight"] = False  # preflight has its own tests (fake provider)

        async def extract(unit):
            call_served_model.set("provider-served-v7")  # what the provider's response said
            return '[{"instruction": "What is on the page?", "response": "Some text about the page."}]'

        async def http_fetch(url):
            if url.endswith("/robots.txt"):
                return 200, "User-agent: *\nAllow: /\nContent-Signal: ai-train=no\n"
            return 404, None

        async def run(c, api_key=None, context=None):
            return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=_Crawler,
                                             _fetch_fn_factory=_site, _extract_fn=extract, _http_fetch=http_fetch)

        code = main.headless_v1_entry(cfg, run=run, out=io.StringIO(), err=io.StringIO(),
                                      environ={"OLLAMA_API_KEY": "dummy"})
        rows = [json.loads(line) for line in (run_dir / "canonical.jsonl").read_text().splitlines()]
        return code, run_dir, rows

    def test_v2_rows_carry_provenance_and_the_run_writes_source_records(self):
        code, run_dir, rows = self.run_entry(2)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 3)
        result = json.loads((run_dir / "result.json").read_text())
        for row in rows:
            p = row["provenance"]
            self.assertEqual((row["schema_version"], row["record_type"]), (2, "qa"))
            self.assertEqual((p["generator"], p["provider"], p["model"], p["pinned_version"], p["served_model"]),
                             ({"name": "qa", "version": 1}, "ollama_cloud", "requested-model",
                              "requested-model@2026-11-01", "provider-served-v7"))
            self.assertEqual(p["endpoint"], "https://ollama.com/v1")
            self.assertEqual((p["run_id"], p["scraper_version"]), (result["run_id"], result["scraper_version"]))
            self.assertEqual(len(p["prompt_sha256"]), 64)
        sources = json.loads((run_dir / "sources.json").read_text())
        self.assertEqual(sources, result["sources"])
        (s,) = sources
        self.assertEqual((s["site"], s["robots_txt"], s["ai_training_signal"], s["licence"]),
                         (HOST, "allowed", "content-signal: ai-train=no", NOTHING_FOUND))

    def test_v1_rows_and_results_are_unchanged(self):
        code, run_dir, rows = self.run_entry(1)
        self.assertEqual(code, 0)
        self.assertTrue(all(list(r) == [*CANONICAL_FIELDS, "scraper_version"] for r in rows))
        self.assertFalse((run_dir / "sources.json").exists())
        self.assertNotIn("sources", json.loads((run_dir / "result.json").read_text()))


if __name__ == "__main__":
    unittest.main()
