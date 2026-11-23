"""Brief 3B Step 4: pluggable generators (item 12). Contract v2 only.

qa@1 must be today's extraction exactly (same messages to the provider, same
rows); code_example@1 proves the interface. Fake provider, site and crawler;
no network, no LLM.
"""

import asyncio
import hashlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

import config
import generators
import headless
import main
from content.extraction import QA_EXTRACTION_SYSTEM_PROMPT, MalformedExtractionError
from crawl import pipeline
from crawl.cost import CostLedger
from export.export import run_export
from generators import PageContext, parse_json_array
from generators.code_example import CodeExampleV1
from generators.qa import QaV1

HOST = "127.0.0.1:9"
GOLDEN = Path(__file__).parent / "golden" / "canonical_v1_main_1112ad7.jsonl"
FROZEN = datetime(2026, 11, 20, 18, 0, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz is not None else FROZEN.replace(tzinfo=None)


class _Crawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _robots(url):
    return (200, "User-agent: *\nAllow: /\n") if url.endswith("/robots.txt") else (404, None)


def page(units=("a",), title="T"):
    return PageContext("https://s.test/p", title, "p", tuple(units))


# ---------------------------------------------------------------------------
# registry and config
# ---------------------------------------------------------------------------
def v2cfg(**extra):
    return {"contract_version": 2, "mode": "crawl", "run_dir": "/nonexistent-scraper-test-run-dir/x",
            "seeds": [["https://s.test/", None]], "host_prefix_pairs": [["s.test", "/"]], **extra}


class TestRegistry(unittest.TestCase):
    def test_default_is_qa_at_1(self):
        self.assertEqual([g.id for g in generators.build()], ["qa@1"])
        self.assertEqual([g.id for g in generators.build([{"name": "code_example", "version": 1},
                                                          {"name": "qa", "version": 1}])],
                         ["code_example@1", "qa@1"])

    def test_config_validation(self):
        headless.validate(v2cfg(generators=[{"name": "qa", "version": 1}, {"name": "code_example", "version": 1}]),
                          environ={})
        for bad, msg in (([], "non-empty list"), ([{"name": "qa"}], "{name, version"),
                         ([{"name": "qa", "version": 2}], "unknown generator 'qa' version 2"),
                         ([{"name": "summary", "version": 1}], "available: qa@1, code_example@1"),
                         ([{"name": "qa", "version": 1}] * 2, "listed twice"),
                         ([{"name": "qa", "version": "1"}], "unknown generator"),
                         ([{"name": "qa", "version": 1, "options": {"x": 1}}], "takes no options")):
            with self.subTest(bad=bad), self.assertRaisesRegex(headless.HeadlessConfigError, msg):
                headless.validate(v2cfg(generators=bad), environ={})

    def test_v1_refuses_the_key(self):
        with self.assertRaisesRegex(headless.HeadlessConfigError, "unknown config keys: generators"):
            headless.validate({**v2cfg(), "contract_version": 1, "generators": [{"name": "qa", "version": 1}]},
                              environ={})


# ---------------------------------------------------------------------------
# qa@1 == today's extraction
# ---------------------------------------------------------------------------
class CapturingLLM:
    def __init__(self):
        self.messages = []

    def invoke(self, messages, **kwargs):
        self.messages.append([(type(m).__name__, m.content) for m in messages])
        msg = AIMessage(content='[{"instruction": "Q?", "response": "A."}]')
        msg.response_metadata["served_model"] = "served"
        return msg


class TestQaV1IsTodaysExtraction(unittest.TestCase):
    def test_the_provider_receives_identical_messages(self):
        unit = "Some unit text. " * 400  # longer than MAX_EXTRACT_CHARS: the cap must match too
        llm = CapturingLLM()
        v1 = asyncio.run(main._make_extract_fn(llm)(unit))
        gen = QaV1()
        v2 = asyncio.run(main._make_llm_call(llm)(*gen.request(unit, page([unit]), 0)))
        self.assertEqual(v1, v2)
        self.assertEqual(llm.messages[0], llm.messages[1])
        self.assertEqual(gen.prompt_sha256, hashlib.sha256(QA_EXTRACTION_SYSTEM_PROMPT.encode()).hexdigest())

    def test_the_prompt_variant_is_honoured(self):
        with patch.object(config, "EXTRACTION_PROMPT_VARIANT", config.ExtractionPromptVariant.CODE_FOCUSED):
            self.assertEqual(QaV1().system_prompt, main._EXTRACTION_SYSTEM_PROMPTS[
                config.ExtractionPromptVariant.CODE_FOCUSED])

    def test_v2_rows_equal_the_golden_v1_rows_field_for_field(self):
        """The golden fixture (tests/golden/make_rows.py) through v2 with qa@1:
        drop the v2 header and provenance, put scraper_version back, and every
        row equals pre-3B main's v1 row, in the same key order."""
        def site(crawler):
            async def fetch(url):
                i = int(url.rstrip("/").rsplit("/p", 1)[1])
                text = f"# Page {i}\n\nPage {i} explains topic {i} in plain words. It has two sentences."
                return text, [f"http://{HOST}/p{i + 1}"] if i < 2 else []
            return fetch

        async def extract(unit):
            n = unit.split("Page ", 1)[1].split()[0]
            return ('[{"instruction": "What does page ' + n + ' explain?", "response": "Topic ' + n
                    + ', in plain words."}, {"instruction": "How many sentences?", "response": "Two, ünïcode kept."}]')

        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"contract_version": 2, "mode": "crawl", "run_dir": tmp, "seeds": [[f"http://{HOST}/p0", None]],
                   "host_prefix_pairs": [[HOST, "/"]], "provider": "ollama_cloud", "model": "golden-model",
                   "max_pages": 3, "crawl_workers": 1, "extract_workers": 1, "intent": "", "run_id": "golden-run",
                   "settings": {"llm_breaker_threshold": 0, "provider_preflight": False}}

            async def runner(c, api_key=None, context=None):
                return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=_Crawler,
                                                 _fetch_fn_factory=site, _extract_fn=extract, _http_fetch=_robots,
                                                 _llm=object())
            with patch.object(pipeline, "datetime", _FrozenDatetime):
                code = main.headless_v1_entry(cfg, run=runner, out=io.StringIO(), err=io.StringIO(),
                                              environ={"OLLAMA_API_KEY": "dummy"},
                                              version={"version": "golden", "commit": "golden", "dirty": False})
            self.assertEqual(code, 0)
            v2_rows = [json.loads(line) for line in (Path(tmp) / "canonical.jsonl").read_text().splitlines()]
        golden = [json.loads(line) for line in GOLDEN.read_text().splitlines()]
        self.assertEqual(len(v2_rows), len(golden))
        for row, want in zip(sorted(v2_rows, key=lambda r: (r["source_url"], r["question"])),
                             sorted(golden, key=lambda r: (r["source_url"], r["question"]))):
            self.assertEqual((row["schema_version"], row["record_type"]), (2, "qa"))
            self.assertEqual(row["provenance"]["generator"], {"name": "qa", "version": 1})
            stripped = {k: v for k, v in row.items() if k not in ("schema_version", "record_type", "provenance")}
            stripped["scraper_version"] = row["provenance"]["scraper_version"]
            self.assertEqual(list(stripped), list(want))
            self.assertEqual(stripped, want)


# ---------------------------------------------------------------------------
# code_example@1
# ---------------------------------------------------------------------------
class TestCodeExampleV1(unittest.TestCase):
    def test_wants_only_units_with_a_fenced_block(self):
        g = CodeExampleV1()
        self.assertTrue(g.wants("Intro\n```python\nx = 1\n```", page(), 0))
        self.assertTrue(g.wants("```\nuvx library-skills\n\n```", page(), 0))
        self.assertFalse(g.wants("Prose with `inline code` only.", page(), 0))

    def test_request_carries_the_preceding_unit_as_capped_context(self):
        g = CodeExampleV1()
        units = ("x" * 5000 + "To run it, use uv:", "```\nuv run app\n```")
        system, text = g.request(units[1], page(units, "Tutorial"), 1)
        self.assertEqual(system, g.system_prompt)
        self.assertIn("CONTEXT (earlier in the page Tutorial):", text)
        self.assertIn("To run it, use uv:", text)
        self.assertTrue(text.endswith("TEXT:\n```\nuv run app\n```"))
        context = text.split(":\n", 1)[1].split("\n\nTEXT:")[0]
        self.assertEqual(len(context), config.CODE_EXAMPLE_CONTEXT_CHARS)
        self.assertEqual(g.request(units[0], page(units), 0)[1], "TEXT:\n" + units[0])  # first unit: no context

    def test_parse_keeps_complete_items_and_defaults(self):
        raw = json.dumps([
            {"instruction": "Start the server", "code": "uv run app", "language": "bash", "explanation": "Runs it."},
            {"instruction": "No code"}, {"code": "x"}, "junk",
            {"instruction": "Print", "code": "print(1)"}])
        self.assertEqual(CodeExampleV1().parse("Here you go:\n" + raw, "", page(), 0), [
            {"instruction": "Start the server", "code": "uv run app", "language": "bash", "explanation": "Runs it."},
            {"instruction": "Print", "code": "print(1)", "language": "unknown", "explanation": ""}])
        self.assertEqual(CodeExampleV1().parse("[]", "", page(), 0), [])
        with self.assertRaises(MalformedExtractionError):
            CodeExampleV1().parse("no json here", "", page(), 0)

    def test_parse_json_array_salvage_order(self):
        self.assertEqual(parse_json_array('```json\n[1, 2]\n```'), [1, 2])
        self.assertEqual(parse_json_array('Sure! [{"a": 1}] Hope it helps.'), [{"a": 1}])


# ---------------------------------------------------------------------------
# whole v2 runs with both generators
# ---------------------------------------------------------------------------
PAGES = {
    "/": ("# Home\n\n" + "Welcome text about the tool. " * 60 + "\n\nInstall it with uv:\n\n```bash\nuv add tool\n```\n\n"
          + "More prose explaining things. " * 60, ["/guide"]),
    "/guide": ("# Guide\n\n" + "A guide paragraph with no code at all. " * 80, []),
}


def _site(crawler):
    async def fetch(url):
        text, links = PAGES[url.split(HOST, 1)[1] or "/"]
        return text, [f"http://{HOST}{link}" for link in links]
    return fetch


class GeneratorRunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        for p in (patch.object(config, "DEFAULT_POLITENESS_DELAY_SECONDS", 0.0),):
            p.start()
            self.addCleanup(p.stop)

    async def llm_call(self, system, text):
        is_code = system == CodeExampleV1.system_prompt
        self.calls.append(("code_example" if is_code else "qa", text))
        if self.malformed and (self.malformed == "all" or self.malformed == ("code_example" if is_code else "qa")):
            return "not json"
        if is_code:
            return json.dumps([{"instruction": "Install the tool", "code": "uv add tool", "language": "bash",
                                "explanation": "Adds the dependency."}])
        return json.dumps([{"instruction": f"Q{len(self.calls)}?", "response": "An answer that is long enough."}])

    def run_v2(self, name, *, gens, dry=False, malformed=None, timing=True):
        self.malformed = malformed
        run_dir = Path(self.tmp.name) / name
        run_dir.mkdir()
        cfg = {"contract_version": 2, "mode": "crawl", "run_dir": str(run_dir),
               "seeds": [[f"http://{HOST}/", None]], "host_prefix_pairs": [[HOST, "/"]],
               "provider": "ollama_cloud", "model": "m", "max_pages": 5, "max_depth": 1, "crawl_workers": 1,
               "extract_workers": 1, "intent": "", "generators": gens,
               "settings": {"llm_breaker_threshold": 0, "provider_preflight": False,
                            "extract_timing_log": timing}}
        if dry:
            cfg["dry_run"] = True

        async def run(c, api_key=None, context=None):
            return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=_Crawler,
                                             _fetch_fn_factory=_site, _llm_call=self.llm_call,
                                             _http_fetch=_robots, _llm=object())

        code = main.headless_v1_entry(cfg, run=run, out=io.StringIO(), err=io.StringIO(),
                                      environ={"OLLAMA_API_KEY": "dummy"})
        result = json.loads((run_dir / "result.json").read_text())
        canonical = run_dir / "canonical.jsonl"
        rows = [json.loads(line) for line in canonical.read_text().splitlines()] if canonical.exists() else []
        timings = run_dir / "timings.jsonl"
        events = [json.loads(line) for line in timings.read_text().splitlines()] if timings.exists() else []
        return code, result, rows, events


BOTH = [{"name": "qa", "version": 1}, {"name": "code_example", "version": 1}]


class TestBothGeneratorsInARun(GeneratorRunCase):
    def test_rows_of_both_types_with_their_own_provenance(self):
        code, result, rows, events = self.run_v2("both", gens=BOTH)
        self.assertEqual(code, 0)
        by_type = {}
        for r in rows:
            by_type.setdefault(r["record_type"], []).append(r)
        self.assertEqual(set(by_type), {"qa", "code_example"})
        (code_row,) = by_type["code_example"]
        self.assertEqual((code_row["instruction"], code_row["code"], code_row["language"]),
                         ("Install the tool", "uv add tool", "bash"))
        self.assertIn("```bash\nuv add tool\n```", code_row["source_chunk"])
        self.assertNotIn("generation_model", code_row)
        self.assertEqual(code_row["provenance"]["generator"], {"name": "code_example", "version": 1})
        self.assertEqual(code_row["provenance"]["prompt_sha256"], CodeExampleV1().prompt_sha256)
        self.assertTrue(all(r["provenance"]["prompt_sha256"] == QaV1().prompt_sha256 for r in by_type["qa"]))
        # calls: qa on every unit, code_example only on the unit with the fence
        qa_calls = [c for c in self.calls if c[0] == "qa"]
        code_calls = [c for c in self.calls if c[0] == "code_example"]
        self.assertEqual(len(code_calls), 1)
        self.assertEqual(len(qa_calls), len(by_type["qa"]))
        # cost rows and timing events carry the generator
        cost = {r.get("generator"): r for r in result["cost"]["by_model"] if r["role"] == "extraction"}
        self.assertEqual((cost["qa@1"]["calls"], cost["code_example@1"]["calls"]), (len(qa_calls), 1))
        calls = [e for e in events if e["event"] == "extract_call"]
        self.assertEqual(sorted({e["generator"] for e in calls}), ["code_example@1", "qa@1"])
        self.assertEqual(result["balance"]["rows_per_source"], {f"{HOST} code_example": 1,
                                                                f"{HOST} qa": len(by_type["qa"])})

    def test_the_dry_pass_counts_calls_per_generator_exactly(self):
        _code, _result, _rows, _events = self.run_v2("real", gens=BOTH)
        real = {"qa@1": sum(c[0] == "qa" for c in self.calls),
                "code_example@1": sum(c[0] == "code_example" for c in self.calls)}
        real_chars = {k: sum(len(t) for g, t in self.calls if f"{g}@1" == k) for k in real}
        self.calls.clear()
        _code, result, _rows, _events = self.run_v2("dry", gens=BOTH, dry=True)
        self.assertEqual(self.calls, [])
        est = result["estimate"]
        self.assertEqual({g["name"] + "@1": g["calls"] for g in est["generators"]}, real)
        self.assertEqual(est["expected_calls"]["extraction"], sum(real.values()))
        prompts = {"qa@1": QaV1().system_prompt, "code_example@1": CodeExampleV1.system_prompt}
        for g in est["generators"]:
            gid = g["name"] + "@1"
            self.assertEqual(g["input_chars"],
                             real_chars[gid] + real[gid] * (len(prompts[gid]) + len(generators.USER_PREFIX)))
        ratios = {g["name"]: g["output_ratio"] for g in est["generators"]}
        self.assertEqual(ratios, {"qa": "measured (qa prompt)", "code_example": "unmeasured: assumed the qa ratio"})

    def test_one_generator_malformed_does_not_fail_the_page(self):
        code, result, rows, _ = self.run_v2("half", gens=BOTH, malformed="code_example")
        self.assertEqual(code, 0)
        self.assertEqual({r["record_type"] for r in rows}, {"qa"})
        self.assertEqual(result["counts_by_status"], {"done": 2})
        cost = {r.get("generator"): r for r in result["cost"]["by_model"] if r["role"] == "extraction"}
        self.assertEqual(cost["code_example@1"]["malformed"], 1)

    def test_a_page_fails_only_when_every_attempted_call_was_malformed(self):
        _code, result, rows, _ = self.run_v2("all", gens=BOTH, malformed="all")
        self.assertEqual(rows, [])
        self.assertNotIn("done", result["counts_by_status"])

    def test_code_only_run_makes_no_calls_on_prose_and_the_prose_page_is_done(self):
        code, result, rows, _ = self.run_v2("code", gens=[{"name": "code_example", "version": 1}])
        self.assertEqual(code, 0)
        self.assertEqual({c[0] for c in self.calls}, {"code_example"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["counts_by_status"], {"done": 2})
        self.assertEqual([r["record_type"] for r in rows], ["code_example"])


class TestCostLedgerGenerator(unittest.TestCase):
    def test_generator_dimension_and_releases(self):
        c = CostLedger("p", "m")
        c.record("extraction", "ok", generator="qa@1")
        c.record("extraction", "ok")
        c.unavailable_release(generator="qa@1")
        rows = c.to_dict()["by_model"]
        self.assertEqual([r.get("generator") for r in rows], [None, "qa@1"])
        self.assertNotIn("generator", rows[0])
        self.assertEqual(rows[1]["unavailable_releases"], 1)


# ---------------------------------------------------------------------------
# exporters
# ---------------------------------------------------------------------------
class TestExportSkipsNonQaRows(unittest.TestCase):
    def write(self, tmp, rows):
        path = Path(tmp) / "canonical.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return str(path)

    def qa(self, i, **extra):
        return {"question": f"What is thing {i}?", "answer": f"Thing {i} is a long enough answer.",
                "source_chunk": "x", "chunk_index": 0, "source_url": f"https://s.test/{i}", "section": str(i),
                "page_title": "T", "generation_model": "m", "extraction_strategy": "per_chunk",
                "timestamp": "t", "crawl_date": "2026-11-23", "license_signal": None, **extra}

    def test_code_rows_are_skipped_and_counted_not_rejected(self):
        code = {"schema_version": 2, "record_type": "code_example", "instruction": "i", "code": "c",
                "language": "bash", "explanation": "", "source_url": "https://s.test/c", "section": "c"}
        rows = [self.qa(i, schema_version=2, record_type="qa") for i in range(4)] + [code, code]
        with tempfile.TemporaryDirectory() as tmp:
            card = run_export(self.write(tmp, rows), str(Path(tmp) / "out"), schema="alpaca",
                              framework="plain-jsonl")
        self.assertEqual(card["skipped_non_qa_rows"], {"code_example": 2})
        self.assertEqual(card["row_counts"]["rejected_by_validation"], 0)
        self.assertEqual(card["row_counts"]["raw_canonical_records"], 4)

    def test_v1_rows_card_has_no_new_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = run_export(self.write(tmp, [self.qa(i) for i in range(4)]), str(Path(tmp) / "out"),
                              schema="alpaca", framework="plain-jsonl")
        self.assertNotIn("skipped_non_qa_rows", card)


if __name__ == "__main__":
    unittest.main()
