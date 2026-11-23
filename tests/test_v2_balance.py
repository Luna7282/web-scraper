"""Brief 3B Step 3: source balancing (item 11). Contract v2 only.

Pure tests of content/balance.py (keep rule, low-value heuristic, overrides,
caps, report), the v2 settings, and whole in-process v2 runs (fake site,
fake extraction, no network, no LLM).
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import headless
import main
from content.balance import (
    Balancer,
    BalanceSettings,
    LowValue,
    detect_low_value,
    override_for,
    rebalance_rows,
    round_robin_keep,
)
from crawl import pipeline

HOST = "127.0.0.1:9"


def rows_for(units: list[int], url="https://site.test/a", rtype="qa"):
    """units[i] rows for chunk i, each tagged (chunk, n)."""
    return [{"source_url": url, "chunk_index": i, "record_type": rtype, "tag": (i, n)}
            for i, count in enumerate(units) for n in range(count)]


# ---------------------------------------------------------------------------
# keep rule
# ---------------------------------------------------------------------------
class TestRoundRobinKeep(unittest.TestCase):
    def test_each_units_first_row_then_second_in_original_order(self):
        rows = rows_for([5, 1, 3])
        kept = round_robin_keep(rows, 5)
        self.assertEqual([r["tag"] for r in kept], [(0, 0), (0, 1), (1, 0), (2, 0), (2, 1)])

    def test_round_robin_stops_mid_round_in_chunk_order(self):
        kept = round_robin_keep(rows_for([3, 3, 3]), 4)
        self.assertEqual([r["tag"] for r in kept], [(0, 0), (0, 1), (1, 0), (2, 0)])

    def test_limits_at_the_edges(self):
        rows = rows_for([2, 2])
        self.assertEqual(round_robin_keep(rows, 10), rows)
        self.assertEqual(round_robin_keep(rows, 0), [])

    def test_deterministic(self):
        rows = rows_for([7, 2, 9, 1, 4])
        self.assertEqual(round_robin_keep(rows, 11), round_robin_keep(list(rows), 11))


# ---------------------------------------------------------------------------
# low-value heuristic
# ---------------------------------------------------------------------------
CHANGELOG = "# Notes\n" + "".join(f"## 0.{n}.0 (2026-01-0{n})\n* fixed a thing\n" for n in range(1, 6))
LISTING = "# All posts\n" + "".join(f"- [Post {n}](/post/{n})\n" for n in range(30)) + "Some text.\n"
PROSE = "# Guide\n## Installing\nRun the installer.\n## Using it\nCall the function with [a link](/x).\n"


class TestDetectLowValue(unittest.TestCase):
    def test_changelog_by_url_title_and_shape_with_reasons(self):
        kind, reasons = detect_low_value("https://s.test/release-notes/", CHANGELOG, "Release Notes")
        self.assertEqual(kind, "changelog")
        self.assertEqual(reasons, ["url path segment 'release-notes'", "title 'Release Notes'",
                                   "5 of 6 headings are version numbers"])

    def test_changelog_shape_alone_is_enough(self):
        self.assertEqual(detect_low_value("https://s.test/docs/x", CHANGELOG, "X")[0], "changelog")

    def test_index_by_segment_pagination_and_link_density(self):
        self.assertEqual(detect_low_value("https://s.test/genindex.html", PROSE)[0], "index")
        kind, reasons = detect_low_value("https://s.test/blog/page/3", PROSE)
        self.assertEqual((kind, reasons), ("index", ["pagination path '/page/3'"]))
        kind, reasons = detect_low_value("https://s.test/blog/", LISTING, "All posts")
        self.assertEqual(kind, "index")
        self.assertEqual(reasons, ["30 of 32 lines are links (94%)"])

    def test_ordinary_pages_are_not_flagged(self):
        self.assertEqual(detect_low_value("https://s.test/tutorial/", PROSE, "Tutorial"), (None, []))
        # whole-segment match only: 'releases-guide' is not 'releases'
        self.assertEqual(detect_low_value("https://s.test/releases-guide/", PROSE, "Guide")[0], None)
        # one or two version-like headings don't make a changelog
        self.assertIsNone(detect_low_value("https://s.test/x", "# X\n## 2.0 migration\n## 1.0 notes\n")[0])


class TestOverrides(unittest.TestCase):
    def test_longest_prefix_wins_and_a_tie_keeps(self):
        o = {"keep": ["/docs/"], "skip": ["/docs/changelog"]}
        self.assertEqual(override_for("https://s.test/docs/changelog/", o), "skip")
        self.assertEqual(override_for("https://s.test/docs/guide", o), "keep")
        self.assertIsNone(override_for("https://s.test/blog/", o))
        self.assertEqual(override_for("https://s.test/x", {"keep": ["/x"], "skip": ["/x"]}), "keep")

    def test_full_url_prefixes(self):
        o = {"skip": ["https://s.test/news"]}
        self.assertEqual(override_for("https://s.test/news/1", o), "skip")
        self.assertIsNone(override_for("https://other.test/news/1", o))


# ---------------------------------------------------------------------------
# Balancer
# ---------------------------------------------------------------------------
class TestBalancer(unittest.TestCase):
    def test_policy_decides_the_action_and_overrides_beat_it(self):
        url = "https://s.test/changelog"
        for policy in ("skip", "cap", "keep"):
            b = Balancer(BalanceSettings(low_value_policy=policy))
            self.assertEqual(b.classify(url, CHANGELOG).action, policy)
        b = Balancer(BalanceSettings(low_value_policy="skip", low_value_overrides={"keep": ["/changelog"]}))
        v = b.classify(url, CHANGELOG)
        self.assertEqual((v.kind, v.action, v.override), ("changelog", "keep", "keep"))
        b = Balancer(BalanceSettings(low_value_overrides={"skip": ["/tutorial/"]}))
        v = b.classify("https://s.test/tutorial/x", PROSE)
        self.assertEqual((v.kind, v.action, v.override), (None, "skip", "skip"))
        self.assertIn("https://s.test/tutorial/x", b.report()["skipped"])

    def test_per_page_cap_records_the_cut(self):
        b = Balancer(BalanceSettings(max_rows_per_page=100))
        rows = rows_for([6] * 27)  # 162 rows, like Brief 2c's root page
        kept = b.apply("https://site.test/a", rows)
        self.assertEqual(len(kept), 100)
        rep = b.report()
        self.assertEqual(rep["capped"], [{"url": "https://site.test/a", "record_type": "qa",
                                          "rule": "max_rows_per_page", "rows_before": 162, "rows_kept": 100}])
        self.assertEqual((rep["rows_generated"], rep["rows_kept"], rep["rows_cut"]), (162, 100, 62))

    def test_low_value_cap_applies_when_smaller(self):
        b = Balancer(BalanceSettings(max_rows_per_page=100, low_value_policy="cap", low_value_max_rows=25))
        v = b.classify("https://s.test/changelog", CHANGELOG)
        kept = b.apply("https://s.test/changelog", rows_for([10] * 40, "https://s.test/changelog"), v)
        self.assertEqual(len(kept), 25)
        rep = b.report()
        self.assertEqual(rep["capped"][0]["rule"], "low_value_max_rows (changelog)")
        self.assertEqual(rep["low_value_pages"]["https://s.test/changelog"]["action"], "cap")

    def test_caps_are_per_record_type(self):
        b = Balancer(BalanceSettings(max_rows_per_page=3))
        rows = rows_for([5]) + rows_for([5], rtype="code_example")
        kept = b.apply("https://site.test/a", rows)
        self.assertEqual(sorted(r["record_type"] for r in kept), ["code_example"] * 3 + ["qa"] * 3)
        self.assertEqual(b.report()["rows_per_source"], {"site.test code_example": 3, "site.test qa": 3})

    def test_per_source_cap_fills_in_completion_order_and_records_cuts(self):
        b = Balancer(BalanceSettings(max_rows_per_source=10))
        self.assertEqual(len(b.apply("https://site.test/1", rows_for([6], "https://site.test/1"))), 6)
        self.assertEqual(len(b.apply("https://site.test/2", rows_for([6], "https://site.test/2"))), 4)
        self.assertEqual(len(b.apply("https://site.test/3", rows_for([6], "https://site.test/3"))), 0)
        self.assertEqual(len(b.apply("https://other.test/1", rows_for([6], "https://other.test/1"))), 6)
        cuts = [(c["url"], c["rule"], c["rows_before"], c["rows_kept"]) for c in b.report()["capped"]]
        self.assertEqual(cuts, [("https://site.test/2", "max_rows_per_source", 6, 4),
                                ("https://site.test/3", "max_rows_per_source", 6, 0)])

    def test_a_resume_counts_rows_already_written_toward_the_source_cap(self):
        b = Balancer(BalanceSettings(max_rows_per_source=10))
        b.seed_written(rows_for([8], "https://site.test/old"))
        self.assertEqual(len(b.apply("https://site.test/new", rows_for([5], "https://site.test/new"))), 2)

    def test_report_largest_page_share_and_ties(self):
        b = Balancer(BalanceSettings(max_rows_per_page=4))
        for name, n in (("b", 9), ("a", 9), ("c", 2)):
            b.apply(f"https://site.test/{name}", rows_for([n], f"https://site.test/{name}"))
        largest = b.report()["largest_page"]
        self.assertEqual(largest, {"url": "https://site.test/a", "rows": 4, "share": 0.4,
                                   "pages_with_that_many_rows": 2})

    def test_no_settings_means_no_change(self):
        b = Balancer(BalanceSettings())
        rows = rows_for([50, 50])
        self.assertEqual(b.apply("https://site.test/a", rows, LowValue(None)), rows)
        self.assertEqual(b.report()["capped"], [])


class TestRebalanceRows(unittest.TestCase):
    def test_offline_recompute_over_canonical_rows(self):
        """The Step 3 measurement path: a dominant changelog page and a big root page."""
        rows = []
        for i in range(40):
            rows += [{"source_url": "https://s.test/release-notes", "chunk_index": i, "page_title": "Release Notes",
                      "source_chunk": f"## 0.{i}.0\n* change"} for _ in range(5)]
        rows += [{"source_url": "https://s.test/", "chunk_index": i % 20, "page_title": "Home",
                  "source_chunk": f"Prose {i % 20}."} for i in range(120)]
        kept, rep = rebalance_rows(rows, BalanceSettings(100, None, "cap", 25))
        self.assertEqual(rep["rows_per_page"], {"https://s.test/": 100, "https://s.test/release-notes": 25})
        self.assertEqual(len(kept), 125)
        kept, rep = rebalance_rows(rows, BalanceSettings(None, None, "skip", None))
        self.assertEqual((len(kept), rep["largest_page"]["share"]), (120, 1.0))
        self.assertIn("https://s.test/release-notes", rep["skipped"])


# ---------------------------------------------------------------------------
# contract v2 settings
# ---------------------------------------------------------------------------
def v2cfg(**extra):
    return {"contract_version": 2, "mode": "crawl", "run_dir": "/nonexistent-scraper-test-run-dir/x",
            "seeds": [["https://s.test/", None]], "host_prefix_pairs": [["s.test", "/"]], **extra}


class TestBalanceSettings(unittest.TestCase):
    def test_v2_defaults(self):
        eff = headless.validate(v2cfg(), environ={}).effective
        self.assertEqual({k: eff[k]["value"] for k in ("max_rows_per_page", "max_rows_per_source",
                                                       "low_value_policy", "low_value_max_rows")},
                         {"max_rows_per_page": 100, "max_rows_per_source": None, "low_value_policy": "cap",
                          "low_value_max_rows": 25})

    def test_v1_refuses_them(self):
        v1 = {**v2cfg(), "contract_version": 1}
        with self.assertRaisesRegex(headless.HeadlessConfigError, "unknown settings: max_rows_per_page"):
            headless.validate({**v1, "settings": {"max_rows_per_page": 5}}, environ={})
        with self.assertRaisesRegex(headless.HeadlessConfigError, "unknown config keys: low_value_overrides"):
            headless.validate({**v1, "low_value_overrides": {"keep": ["/x"]}}, environ={})
        self.assertNotIn("max_rows_per_page", headless.validate(v1, environ={}).effective)

    def test_bad_values_are_refused(self):
        for settings, msg in (({"low_value_policy": "drop"}, "must be one of"),
                              ({"max_rows_per_page": 0}, ">= 1"), ({"low_value_max_rows": "5"}, "integer")):
            with self.assertRaisesRegex(headless.HeadlessConfigError, msg):
                headless.validate(v2cfg(settings=settings), environ={})
        for bad in ({"keep": "/x"}, {"drop": ["/x"]}, {"skip": [""]}, ["/x"]):
            with self.assertRaisesRegex(headless.HeadlessConfigError, "low_value_overrides"):
                headless.validate(v2cfg(low_value_overrides=bad), environ={})

    def test_null_turns_a_cap_off_from_config_or_environment(self):
        eff = headless.validate(v2cfg(settings={"max_rows_per_page": None}), environ={}).effective
        self.assertIsNone(eff["max_rows_per_page"]["value"])
        eff = headless.validate(v2cfg(), environ={"SCRAPER_MAX_ROWS_PER_PAGE": "none"}).effective
        self.assertEqual(eff["max_rows_per_page"], {"value": None, "source": "env"})
        eff = headless.validate(v2cfg(), environ={"SCRAPER_LOW_VALUE_POLICY": "skip"}).effective
        self.assertEqual(eff["low_value_policy"], {"value": "skip", "source": "env"})


# ---------------------------------------------------------------------------
# whole v2 runs, in process
# ---------------------------------------------------------------------------
class _Crawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


PAGES = {
    "/": ("# Home\n\n" + "\n\n".join(f"Section {k}. " + "Words about the product. " * 30 for k in range(6)),
          ["/changelog", "/guide"]),
    "/changelog": ("# Changelog\n\n" + "\n\n".join(f"## 1.{n}.0\n\n" + "* fixed something. " * 20
                                                   for n in range(6)), []),
    "/guide": ("# Guide\n\n" + "\n\n".join(f"Step {k}. " + "Do this then that. " * 30 for k in range(4)), []),
}


def _site(crawler):
    async def fetch(url):
        path = url.split(HOST, 1)[1] or "/"
        text, links = PAGES[path]
        return text, [f"http://{HOST}{link}" for link in links]
    return fetch


async def _robots(url):
    return (200, "User-agent: *\nAllow: /\n") if url.endswith("/robots.txt") else (404, None)


class BalanceRunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.extracted = []
        p = patch.object(config, "DEFAULT_POLITENESS_DELAY_SECONDS", 0.0)
        p.start()
        self.addCleanup(p.stop)

    async def extract(self, unit):
        self.extracted.append(unit)
        n = len(self.extracted)
        return json.dumps([{"instruction": f"Question {n}.{k}?", "response": f"A long enough answer {n}.{k}."}
                           for k in range(4)])

    def run_v2(self, name, *, dry=False, settings=None, overrides=None):
        run_dir = Path(self.tmp.name) / name
        run_dir.mkdir()
        cfg = {"contract_version": 2, "mode": "crawl", "run_dir": str(run_dir),
               "seeds": [[f"http://{HOST}/", None]], "host_prefix_pairs": [[HOST, "/"]],
               "provider": "ollama_cloud", "model": "m", "max_pages": 5, "max_depth": 1, "crawl_workers": 1,
               "extract_workers": 1, "intent": "",
               "settings": {"llm_breaker_threshold": 0, "provider_preflight": False, **(settings or {})}}
        if dry:
            cfg["dry_run"] = True
        if overrides is not None:
            cfg["low_value_overrides"] = overrides

        async def run(c, api_key=None, context=None):
            return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=_Crawler,
                                             _fetch_fn_factory=_site, _extract_fn=self.extract,
                                             _http_fetch=_robots, _llm=object())

        code = main.headless_v1_entry(cfg, run=run, out=io.StringIO(), err=io.StringIO(),
                                      environ={"OLLAMA_API_KEY": "dummy"})
        result = json.loads((run_dir / "result.json").read_text())
        canonical = run_dir / "canonical.jsonl"
        rows = [json.loads(line) for line in canonical.read_text().splitlines()] if canonical.exists() else []
        return code, result, rows


class TestBalanceInARun(BalanceRunCase):
    def test_caps_and_low_value_cap_reach_the_written_rows_and_result_json(self):
        code, result, rows = self.run_v2("cap", settings={"max_rows_per_page": 6, "low_value_max_rows": 2})
        self.assertEqual(code, 0)
        per_page = {}
        for r in rows:
            per_page[r["source_url"]] = per_page.get(r["source_url"], 0) + 1
        bal = result["balance"]
        self.assertEqual(per_page, {k: v for k, v in bal["rows_per_page"].items() if v})
        self.assertEqual(bal["rows_per_page"][f"http://{HOST}/changelog"], 2)
        self.assertTrue(all(n <= 6 for n in per_page.values()))
        self.assertEqual(bal["low_value_pages"][f"http://{HOST}/changelog"]["kind"], "changelog")
        self.assertEqual(bal["rows_kept"], len(rows))
        self.assertEqual(bal["rows_generated"] - bal["rows_cut"], len(rows))
        self.assertGreater(bal["largest_page"]["share"], 0)
        self.assertEqual({c["rule"] for c in bal["capped"]}, {"max_rows_per_page", "low_value_max_rows (changelog)"})

    def test_skip_makes_no_extraction_calls_for_the_page_and_an_override_restores_it(self):
        _code, result, rows = self.run_v2("skip", settings={"low_value_policy": "skip"})
        self.assertFalse(any("fixed something" in u for u in self.extracted))
        self.assertNotIn(f"http://{HOST}/changelog", {r["source_url"] for r in rows})
        self.assertIn(f"http://{HOST}/changelog", result["balance"]["skipped"])
        self.assertEqual(result["counts_by_status"].get("skipped_extract"), 1)
        self.extracted.clear()
        _code, result, rows = self.run_v2("override", settings={"low_value_policy": "skip"},
                                          overrides={"keep": ["/changelog"]})
        self.assertTrue(any("fixed something" in u for u in self.extracted))
        self.assertIn(f"http://{HOST}/changelog", {r["source_url"] for r in rows})
        self.assertEqual(result["balance"]["low_value_pages"][f"http://{HOST}/changelog"]["override"], "keep")

    def test_the_dry_pass_leaves_skipped_pages_out_of_the_estimate(self):
        _code, result, _rows = self.run_v2("dry", dry=True, settings={"low_value_policy": "skip"})
        est = result["estimate"]
        self.assertIn("low-value page (changelog)", est["pages_skipped"][f"http://{HOST}/changelog"])
        self.assertEqual(est["units_per_page"][f"http://{HOST}/changelog"], 0)  # as for relevance skips
        self.assertIn(f"http://{HOST}/changelog", result["balance"]["skipped"])
        self.assertEqual(self.extracted, [])

    def test_v1_runs_have_no_balance_section(self):
        run_dir = Path(self.tmp.name) / "v1"
        run_dir.mkdir()
        cfg = {"contract_version": 1, "mode": "crawl", "run_dir": str(run_dir),
               "seeds": [[f"http://{HOST}/", None]], "host_prefix_pairs": [[HOST, "/"]],
               "provider": "ollama_cloud", "model": "m", "max_pages": 5, "max_depth": 1, "crawl_workers": 1,
               "extract_workers": 1, "intent": "", "settings": {"llm_breaker_threshold": 0}}

        async def run(c, api_key=None, context=None):
            with patch.object(pipeline, "DEFAULT_RATE_LIMIT_BACKOFF_SECONDS", 0.01):
                return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=_Crawler,
                                                 _fetch_fn_factory=_site, _extract_fn=self.extract,
                                                 _http_fetch=_robots, _llm=object())

        code = main.headless_v1_entry(cfg, run=run, out=io.StringIO(), err=io.StringIO(),
                                      environ={"OLLAMA_API_KEY": "dummy"})
        result = json.loads((run_dir / "result.json").read_text())
        self.assertEqual(code, 0)
        self.assertNotIn("balance", result)
        rows = (run_dir / "canonical.jsonl").read_text().splitlines()
        self.assertGreater(len(rows), 6)  # nothing capped in v1


if __name__ == "__main__":
    unittest.main()
