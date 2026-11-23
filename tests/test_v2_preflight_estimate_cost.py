"""Brief 3B Step 2: provider and embeddings preflight (items 19, 21), the dry
pass (estimate.json) and cost accounting (item 20). Contract v2 only.

Fake provider / embeddings / site / crawler throughout; no network (the test
network guard enforces it), no LLM.
"""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import requests
from langchain_core.messages import AIMessage

import config
import headless
import main
from crawl import pipeline
from crawl.cost import CostLedger
from crawl.pipeline import ProviderUnavailableError, RateLimitError
from crawl.timing import call_usage
from preflight import (
    PreflightFailed,
    classify_provider_error,
    embeddings_preflight,
    needs_embeddings,
    provider_preflight,
)

HOST = "127.0.0.1:9"


class StatusError(Exception):
    def __init__(self, status, body=""):
        super().__init__(body or f"HTTP {status}")
        self.status_code = status


class FakeLLM:
    def __init__(self, error=None, served="served-v1"):
        self.error, self.served, self.calls = error, served, 0

    def invoke(self, messages, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        msg = AIMessage(content="OK", usage_metadata={"input_tokens": 9, "output_tokens": 1, "total_tokens": 10})
        msg.response_metadata["served_model"] = self.served
        return msg


class FakeEmbeddings:
    def __init__(self, error=None):
        self.error = error

    def embed_query(self, text):
        if self.error is not None:
            raise self.error
        return [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------
# preflight units
# ---------------------------------------------------------------------------
class TestClassify(unittest.TestCase):
    def test_retired_auth_and_everything_else(self):
        cases = [(StatusError(410, "model retired"), "retired"),
                 (StatusError(404, "model 'deepseek-v4-flash' not found"), "retired"),
                 (StatusError(404, "Error: The model `x` does not exist"), "retired"),
                 (StatusError(404, "page not found"), "other"),  # a 404 that doesn't name a model
                 (StatusError(401), "auth"), (StatusError(403), "auth"),
                 (StatusError(429), "other"), (StatusError(503), "other"), (TimeoutError("t"), "other")]
        for error, kind in cases:
            with self.subTest(error=str(error)):
                self.assertEqual(classify_provider_error(error), kind)


class TestProviderPreflight(unittest.TestCase):
    def run_preflight(self, llm, cost=None):
        return asyncio.run(provider_preflight(llm, "ollama_cloud", "m", cost))

    def test_ok_is_one_tiny_call_recorded_in_cost(self):
        llm, cost = FakeLLM(), CostLedger("ollama_cloud", "m")
        info = self.run_preflight(llm, cost)
        self.assertEqual((info["status"], info["served_model"], llm.calls), ("ok", "served-v1", 1))
        self.assertEqual(llm.kwargs, {"max_tokens": config.PREFLIGHT_MAX_TOKENS})
        (row,) = cost.to_dict()["by_model"]
        self.assertEqual((row["role"], row["calls"], row["ok"], row["input_tokens"]), ("preflight", 1, 1, 9))

    def test_a_retired_model_fails_with_repick(self):
        for error in (StatusError(410, "gone"), StatusError(404, "model 'old' not found")):
            with self.subTest(status=error.status_code), self.assertRaises(PreflightFailed) as ctx:
                self.run_preflight(FakeLLM(error))
            self.assertIn("repick the model", str(ctx.exception))

    def test_a_rejected_key_fails(self):
        for status in (401, 403):
            with self.subTest(status=status), self.assertRaises(PreflightFailed) as ctx:
                self.run_preflight(FakeLLM(StatusError(status, "invalid key sk-" + "x" * 30)))
            self.assertIn("rejected the API key", str(ctx.exception))
            self.assertNotIn("sk-x", str(ctx.exception))  # no key-shaped text in the message

    def test_transient_errors_are_warnings(self):
        for error in (StatusError(429), StatusError(503), TimeoutError("slow")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.run_preflight(FakeLLM(error))["status"], "warning")


class TestEmbeddingsPreflight(unittest.TestCase):
    def run_preflight(self, embeddings):
        return asyncio.run(embeddings_preflight(embeddings, "nomic-embed-text"))

    def test_ok(self):
        self.assertEqual(self.run_preflight(FakeEmbeddings())["dim"], 3)

    def test_failures_name_the_problem_and_the_alternatives(self):
        cases = [(requests.exceptions.ConnectionError("refused"), "not reachable at localhost:11434"),
                 (StatusError(404, 'model "nomic-embed-text" not found'), "is not available in local Ollama"),
                 (RuntimeError("boom"), "embeddings preflight failed")]
        for error, fragment in cases:
            with self.subTest(fragment=fragment), self.assertRaises(PreflightFailed) as ctx:
                self.run_preflight(FakeEmbeddings(error))
            message = str(ctx.exception)
            self.assertIn(fragment, message)
            self.assertIn("ollama pull nomic-embed-text", message)
            self.assertIn("blank intent", message)

    def test_which_runs_need_embeddings(self):
        self.assertFalse(needs_embeddings({"intent": ""}))
        self.assertTrue(needs_embeddings({"intent": "x"}))
        self.assertTrue(needs_embeddings({"build_rag": True}))
        with patch.object(config, "EXTRACTION_STRATEGY", config.ExtractionStrategy.TOP_K_CHUNKS_BY_RELEVANCE):
            self.assertTrue(needs_embeddings({}))


class TestCostLedger(unittest.TestCase):
    def test_tokens_are_null_when_never_reported_never_zero(self):
        ledger = CostLedger("p", "m")
        ledger.record("extraction", "ok", None)
        ledger.record("extraction", "rate_limited")
        totals = ledger.to_dict()["totals"]
        self.assertEqual((totals["calls"], totals["ok"], totals["rate_limited"]), (2, 1, 1))
        self.assertIsNone(totals["input_tokens"])
        self.assertEqual(totals["usage_missing_calls"], 2)

    def test_reported_usage_is_summed_per_role(self):
        ledger = CostLedger("p", "m")
        ledger.record("extraction", "ok", {"input_tokens": 10, "output_tokens": 30, "reasoning_tokens": None})
        ledger.record("extraction", "malformed", {"input_tokens": 5, "output_tokens": 7, "reasoning_tokens": 2})
        ledger.record("extraction", "ok", None)
        ledger.unavailable_release()
        ledger.record("preflight", "ok", {"input_tokens": 9, "output_tokens": 1})
        data = ledger.to_dict()
        extraction = next(r for r in data["by_model"] if r["role"] == "extraction")
        self.assertEqual((extraction["calls"], extraction["input_tokens"], extraction["output_tokens"],
                          extraction["reasoning_tokens"], extraction["usage_missing_calls"],
                          extraction["unavailable_releases"]), (3, 15, 37, 2, 1, 1))
        self.assertEqual((data["totals"]["input_tokens"], data["totals"]["calls"]), (24, 4))


class TestV2KeysStep2(unittest.TestCase):
    BASE: ClassVar[dict] = {"mode": "crawl", "run_dir": "/nonexistent-scraper-test-run-dir/x",
            "seeds": [["https://x.test/", None]], "host_prefix_pairs": [["x.test", "/"]]}

    def test_dry_run_and_preflight_settings_are_v2_only(self):
        hr = headless.validate({**self.BASE, "contract_version": 2, "dry_run": True,
                                "settings": {"provider_preflight": False}}, {})
        self.assertEqual(hr.effective["provider_preflight"], {"value": False, "source": "config"})
        self.assertEqual(hr.effective["embeddings_preflight"], {"value": True, "source": "default"})
        with self.assertRaises(headless.HeadlessConfigError):
            headless.validate({**self.BASE, "contract_version": 1, "dry_run": True}, {})
        with self.assertRaises(headless.HeadlessConfigError):
            headless.validate({**self.BASE, "contract_version": 1, "settings": {"provider_preflight": False}}, {})
        self.assertNotIn("provider_preflight", headless.validate({**self.BASE, "contract_version": 1}, {}).effective)
        with self.assertRaises(headless.HeadlessConfigError):
            headless.validate({**self.BASE, "contract_version": 2, "dry_run": True, "resume": True}, {})


# ---------------------------------------------------------------------------
# whole v2 runs, in process
# ---------------------------------------------------------------------------
class _Crawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FailIfEntered:
    async def __aenter__(self):
        raise AssertionError("page work started despite a failed preflight")

    async def __aexit__(self, *exc):
        return False


def page_markdown(i: int) -> str:
    """Several prose paragraphs and fenced code blocks: the real chunker
    splits it into many units, differently per page."""
    parts = [f"# Page {i}\n"]
    for k in range(3 + i):
        parts.append(f"Paragraph {k} of page {i}. " + "This sentence explains a concept in detail. " * (8 + k))
        parts.append(f"```python\ndef example_{i}_{k}(x):\n    return x * {k}\n```")
    return "\n\n".join(parts)


def _site(crawler):
    async def fetch(url):
        i = int(url.rsplit("/p", 1)[1])
        return page_markdown(i), [f"http://{HOST}/p{i + 1}"] if i < 2 else []
    return fetch


async def _robots(url):
    return (200, "User-agent: *\nAllow: /\n") if url.endswith("/robots.txt") else (404, None)


class V2RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = 0
        p1 = patch.object(pipeline, "DEFAULT_RATE_LIMIT_BACKOFF_SECONDS", 0.01)
        p2 = patch.object(config, "DEFAULT_POLITENESS_DELAY_SECONDS", 0.0)
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)

    def run_v2(self, name, *, extract=None, llm=None, embeddings=None, crawler=_Crawler, dry=False, settings=None,
               intent="", resume=False):
        run_dir = Path(self.tmp.name) / name
        run_dir.mkdir(exist_ok=True)
        cfg = {"contract_version": 2, "mode": "crawl", "run_dir": str(run_dir),
               "seeds": [[f"http://{HOST}/p0", None]], "host_prefix_pairs": [[HOST, "/"]],
               "provider": "ollama_cloud", "model": "m", "max_pages": 3, "crawl_workers": 1,
               "extract_workers": 1, "intent": intent,
               "settings": {"llm_breaker_threshold": 0, **(settings or {})}}
        if dry:
            cfg["dry_run"] = True
        if resume:
            cfg["resume"] = True

        async def run(c, api_key=None, context=None):
            return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=crawler,
                                             _fetch_fn_factory=_site, _extract_fn=extract, _http_fetch=_robots,
                                             _llm=llm, _embeddings=embeddings)

        err = io.StringIO()
        code = main.headless_v1_entry(cfg, run=run, out=io.StringIO(), err=err, environ={"OLLAMA_API_KEY": "dummy"})
        result_path = run_dir / "result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else None
        return code, result, run_dir, err.getvalue()


class TestPreflightInARun(V2RunCase):
    def test_a_retired_model_fails_fast_with_exit_2_before_any_page_work(self):
        llm = FakeLLM(StatusError(410, "model gone"))
        code, result, run_dir, err = self.run_v2("retired", llm=llm, crawler=_FailIfEntered)
        self.assertEqual(code, headless.EXIT_CONFIG_ERROR)
        self.assertEqual((result["outcome"], result["error"]["type"]), ("config_error", "PreflightFailed"))
        self.assertIn("repick the model", result["error"]["message"])
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(err.startswith("config error: provider preflight"))
        self.assertFalse((run_dir / "frontier.db").exists())  # no page work at all
        self.assertEqual(llm.calls, 1)

    def test_missing_embeddings_fail_fast_when_the_run_needs_them(self):
        code, result, _run_dir, _ = self.run_v2(
            "noembed", llm=FakeLLM(), embeddings=FakeEmbeddings(requests.exceptions.ConnectionError("x")),
            crawler=_FailIfEntered, intent="python decorators")
        self.assertEqual((code, result["outcome"]), (headless.EXIT_CONFIG_ERROR, "config_error"))
        self.assertIn("not reachable", result["error"]["message"])


class TestCostInARun(V2RunCase):
    def test_result_json_accounts_for_every_call(self):
        def extract_fake():
            async def extract(unit):
                self.calls += 1
                if self.calls == 2:
                    raise RateLimitError()
                if self.calls == 4:
                    raise ProviderUnavailableError(status_code=503)
                call_usage.set({"input_tokens": 10, "output_tokens": 25, "total_tokens": 35, "reasoning_tokens": None}
                               if self.calls % 3 else None)  # every third successful call reports no usage
                return '[{"instruction": "Q?", "response": "An answer that is long enough."}]'
            return extract

        code, result, _run_dir, _ = self.run_v2("cost", extract=extract_fake(), llm=FakeLLM())
        self.assertEqual(code, 0)
        cost = result["cost"]
        rows = {r["role"]: r for r in cost["by_model"]}
        self.assertEqual(set(rows), {"preflight", "extraction"})
        ex = rows["extraction"]
        self.assertEqual(ex["calls"], self.calls)
        self.assertEqual((ex["rate_limited"], ex["unavailable"], ex["unavailable_releases"]), (1, 1, 1))
        ok_calls = ex["ok"] + ex["malformed"]
        self.assertEqual(ok_calls, self.calls - 2)
        reported = ex["usage_reported_calls"]
        self.assertEqual((ex["input_tokens"], ex["output_tokens"]), (10 * reported, 25 * reported))
        self.assertIsNone(ex["reasoning_tokens"])  # never reported: null, not 0
        self.assertEqual(ex["usage_missing_calls"], self.calls - reported)
        self.assertEqual(result["preflight"]["provider"]["status"], "ok")
        self.assertEqual(cost["totals"]["calls"], self.calls + 1)


class TestDryPass(V2RunCase):
    def real_calls_per_page(self):
        seen = {}

        async def extract(unit):
            self.calls += 1
            page = unit.split("of page ", 1)[1].split(".")[0] if "of page " in unit else unit.split()[2]
            seen[page] = seen.get(page, 0) + 1
            return "[]"

        code, result, _run_dir, _ = self.run_v2("real", extract=extract, llm=object(),
                                               settings={"provider_preflight": False})
        self.assertEqual(code, 0)
        return self.calls, result

    def test_zero_llm_calls_and_the_provider_is_never_constructed(self):
        def never(*a, **k):
            raise AssertionError("the dry pass constructed the chat provider")

        with patch.object(main, "get_llm", never), patch("llm_factory.ServedModelChatOpenAI.__init__", never), \
                patch.object(main, "_make_extract_fn", never):
            code, result, run_dir, _ = self.run_v2("dry", dry=True)
        self.assertEqual(code, 0)
        self.assertTrue(result["dry_run"])
        self.assertNotIn("cost", result)
        self.assertFalse((run_dir / "canonical.jsonl").exists())
        estimate = json.loads((run_dir / "estimate.json").read_text())
        self.assertEqual(estimate, result["estimate"])
        self.assertEqual(estimate["pages"], 3)
        self.assertGreater(estimate["total_units"], 3)
        self.assertIn("Brief 2c real run", estimate["token_estimate"]["basis"])
        self.assertEqual(estimate["expected_calls"]["preflight"], 1)
        self.assertEqual(result["counts_by_status"], {"mapped": 3})
        # Fingerprints: one per page, one sha per unit, sizes matching units_per_page.
        prints = estimate["page_fingerprints"]
        self.assertEqual(set(prints), set(estimate["units_per_page"]))
        for url, fp in prints.items():
            self.assertRegex(fp["content_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(len(fp["unit_chars"]), estimate["units_per_page"][url])
            self.assertEqual(len(fp["unit_sha256"]), len(fp["unit_chars"]))

    def test_the_dry_pass_unit_count_equals_a_real_runs_calls(self):
        """Same fixture pages, same chunker: estimate.total_units == the
        number of extraction calls a real run makes."""
        real_calls, _ = self.real_calls_per_page()
        _code, result, _run_dir, _ = self.run_v2("dry2", dry=True)
        self.assertEqual(result["estimate"]["total_units"], real_calls)
        tokens = result["estimate"]["token_estimate"]
        self.assertEqual(tokens["output_tokens"], round(tokens["input_tokens"] * config.ESTIMATE_OUTPUT_PER_INPUT_TOKEN))

    def test_a_real_run_is_refused_in_a_dry_pass_run_dir(self):
        self.run_v2("drydir", dry=True)
        code, result, _run_dir, err = self.run_v2("drydir", extract=None, llm=FakeLLM(), resume=True)
        self.assertEqual(code, headless.EXIT_CONFIG_ERROR)
        self.assertIn("holds a dry pass", err)
        self.assertTrue(result["dry_run"])  # the dry pass's result.json is untouched


if __name__ == "__main__":
    unittest.main()
