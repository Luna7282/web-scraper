"""Brief 3B Step 5: verifier hook (item 13). Contract v2 only.

Generic verifiers (schema, near_duplicate, code_syntax -- none executes
anything), the external command hook (no shell, own process group, minimal
environment, temp cwd, timeout, stdout cap, concurrency limit), the runner
(drop / flag, statuses, counts) and whole in-process v2 runs. No network.
"""

import asyncio
import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import headless
import main
import verifiers
from generators.code_example import CodeExampleV1
from verifiers import PageState, Verdict, Verifier, VerifierRunner
from verifiers.code_syntax import CodeSyntaxV1
from verifiers.external import ExternalV1
from verifiers.near_duplicate import NearDuplicateV1
from verifiers.schema import SchemaV1

HOST = "127.0.0.1:9"


def run(coro):
    return asyncio.run(coro)


def qa_row(q="What does the tool do?", a="It crawls sites and builds datasets.", url="https://s.test/a", chunk=0):
    return {"schema_version": 2, "record_type": "qa", "question": q, "answer": a, "source_chunk": "x",
            "chunk_index": chunk, "source_url": url, "page_title": "T",
            "provenance": {"generator": {"name": "qa", "version": 1}}}


def code_row(code, unit, context="", url="https://s.test/a", chunk=1, title="Tutorial"):
    return {"schema_version": 2, "record_type": "code_example", "instruction": "Start the dev server",
            "code": code, "language": "bash", "explanation": "Runs it.", "context": context,
            "source_chunk": unit, "chunk_index": chunk, "source_url": url, "page_title": title,
            "provenance": {"generator": {"name": "code_example", "version": 1}}}


def verify(verifier, row, page=None):
    return run(verifier.verify(row, page or PageState(row.get("source_url", "u"))))


# ---------------------------------------------------------------------------
# schema@1
# ---------------------------------------------------------------------------
class TestSchemaV1(unittest.TestCase):
    def test_qa_rows_follow_schema_and_export_rules(self):
        self.assertEqual(verify(SchemaV1(), qa_row()), Verdict.ok())
        self.assertEqual(verify(SchemaV1(), qa_row(a="Too short.")), Verdict.fail("answer_too_short"))
        self.assertEqual(verify(SchemaV1(), qa_row(q="Same text here please", a="same text here please")),
                         Verdict.fail("question_equals_answer"))
        row = qa_row()
        del row["answer"]
        self.assertEqual(verify(SchemaV1(), row).reasons, ("missing field 'answer'",))

    def test_unknown_generator_is_unknown_and_wrong_type_fails(self):
        row = qa_row()
        row["provenance"]["generator"] = {"name": "summary", "version": 9}
        self.assertEqual(verify(SchemaV1(), row).verdict, "unknown")
        row = qa_row()
        row["record_type"] = "code_example"
        self.assertEqual(verify(SchemaV1(), row).verdict, "fail")

    def test_hallucinated_code_fails(self):
        unit = "```\nuv run fastapi dev\n```"
        v = verify(SchemaV1(), code_row("pip install fastapi && fastapi run --reload", unit, "Use uv to run it:"))
        self.assertEqual(v.verdict, "fail")
        self.assertIn("does not appear in the text sent", v.reasons[0])

    def test_code_found_only_in_the_sent_context_passes(self):
        """The preceding unit was sent as context; code copied from it is grounded."""
        context = "Install it first:\n\n    uv add fastapi[standard]\n\nthen run the app."
        unit = "```\nuv run fastapi dev\n```"
        row = code_row("uv add fastapi[standard]", unit, context)
        self.assertEqual(verify(SchemaV1(), row), Verdict.ok())
        self.assertNotIn("uv add fastapi", unit)
        # repeatable from the row alone: the rebuilt request is exactly what the generator sent
        page = __import__("generators").PageContext(row["source_url"], "Tutorial", None, (context, unit))
        _system, text = CodeExampleV1().request(unit, page, 1)
        self.assertEqual(CodeExampleV1.sent_text(row), "Text to process:\n\n" + text)
        self.assertEqual(CodeExampleV1().parse('[{"instruction": "i", "code": "c"}]', unit, page, 1)[0]["context"],
                         context)

    def test_whitespace_differences_are_tolerated_but_text_outside_what_was_sent_is_not(self):
        unit = "```python\ndef f(x):\n    return x * 2\n```"
        self.assertEqual(verify(SchemaV1(), code_row("def f(x):\n  return x * 2", unit)), Verdict.ok())
        # text beyond the MAX_EXTRACT_CHARS cap was never sent
        long_unit = "a" * config.MAX_EXTRACT_CHARS + "\nsecret_tail_code()"
        self.assertEqual(verify(SchemaV1(), code_row("secret_tail_code()", long_unit)).verdict, "fail")


# ---------------------------------------------------------------------------
# near_duplicate@1
# ---------------------------------------------------------------------------
class TestNearDuplicateV1(unittest.TestCase):
    def test_within_a_page_the_earliest_row_wins(self):
        runner = VerifierRunner([(NearDuplicateV1(), "flag")])
        rows = [qa_row("How do I install it?", chunk=0), qa_row("How do I install it", chunk=3),
                qa_row("How do I configure logging?", chunk=4)]
        out = run(runner.run("https://s.test/a", rows))
        self.assertEqual([r["verification_status"] for r in out], ["pass", "fail", "pass"])
        self.assertIn("https://s.test/a chunk 0", out[1]["verification"][0]["reasons"][0])

    def test_across_pages_only_committed_rows_count(self):
        nd = NearDuplicateV1()
        runner = VerifierRunner([(nd, "flag")])
        first = run(runner.run("https://s.test/a", [qa_row("What is a frontier database?")]))
        # not committed (e.g. cut by a cap): a later duplicate still passes
        out = run(runner.run("https://s.test/b", [qa_row("What is a frontier database?", url="https://s.test/b")]))
        self.assertEqual(out[0]["verification_status"], "pass")
        runner.commit(first)
        out = run(runner.run("https://s.test/c", [qa_row("what is a frontier database", url="https://s.test/c")]))
        self.assertEqual(out[0]["verification_status"], "fail")

    def test_similarity_threshold_and_record_types_kept_apart(self):
        nd = NearDuplicateV1()
        nd.commit([qa_row("how do i run the development server with uv today")])
        near = qa_row("how do i run the development server with uv today please")  # 10 of 11 tokens
        self.assertEqual(verify(nd, near).verdict, "fail")
        self.assertEqual(verify(NearDuplicateV1({"threshold": 0.95}), near).verdict, "pass")
        code = code_row("uv run app", "```\nuv run app\n```")
        code["instruction"] = "how do i run the development server with uv today"
        self.assertEqual(verify(nd, code).verdict, "pass")  # another record type
        with self.assertRaises(ValueError):
            NearDuplicateV1({"threshold": 1.5})


# ---------------------------------------------------------------------------
# code_syntax@1
# ---------------------------------------------------------------------------
class TestCodeSyntaxV1(unittest.TestCase):
    def row(self, code, language):
        r = code_row(code, code)
        r["language"] = language
        return r

    def test_python_json_and_others(self):
        self.assertEqual(verify(CodeSyntaxV1(), self.row("def f(x):\n    return x\n", "python")), Verdict.ok())
        v = verify(CodeSyntaxV1(), self.row("def f(x)\n    return x\n", "Python"))
        self.assertEqual(v.verdict, "fail")
        self.assertIn("line 1", v.reasons[0])
        self.assertEqual(verify(CodeSyntaxV1(), self.row(">>> x = 1\n>>> x + 1\n2\n", "pycon")), Verdict.ok())
        self.assertEqual(verify(CodeSyntaxV1(), self.row('{"a": 1}', "json")), Verdict.ok())
        self.assertEqual(verify(CodeSyntaxV1(), self.row('{"a": }', "json")).verdict, "fail")
        self.assertEqual(verify(CodeSyntaxV1(), self.row("uv run app", "bash")),
                         Verdict.unknown("no parser for bash"))
        self.assertFalse(CodeSyntaxV1().applies(qa_row()))

    def test_it_never_executes_the_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "ran"
            code = f"open({str(marker)!r}, 'w').write('x')\nimport os\nos._exit(3)\n"
            self.assertEqual(verify(CodeSyntaxV1(), self.row(code, "python")), Verdict.ok())
            self.assertFalse(marker.exists())


# ---------------------------------------------------------------------------
# external@1
# ---------------------------------------------------------------------------
class ExternalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def script(self, body: str) -> list[str]:
        path = Path(self.tmp.name) / f"s{len(list(Path(self.tmp.name).iterdir()))}.py"
        path.write_text(textwrap.dedent(body))
        return [sys.executable, str(path)]


class TestExternalV1(ExternalCase):
    def test_pass_and_fail_verdicts_from_stdin_row(self):
        cmd = self.script("""
            import json, sys
            row = json.loads(sys.stdin.read())
            ok = "rm -rf" not in row["code"]
            print(json.dumps({"verdict": "pass" if ok else "fail", "reasons": [] if ok else ["dangerous"]}))
        """)
        ext = ExternalV1({"command": cmd})
        self.assertEqual(verify(ext, code_row("uv run app", "u")), Verdict.ok())
        self.assertEqual(verify(ext, code_row("rm -rf /", "u")), Verdict.fail("dangerous"))

    def test_minimal_environment_and_an_empty_temporary_working_directory(self):
        cmd = self.script("""
            import json, os
            print(json.dumps({"verdict": "pass", "reasons": [json.dumps(sorted(os.environ)), os.getcwd(),
                                                             json.dumps(os.listdir("."))]}))
        """)
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "secret-value", "FOUNDRY_SCRAPER_API_KEY": "x"}):
            v = verify(ExternalV1({"command": cmd}), qa_row())
        env_keys, cwd, listing = json.loads(v.reasons[0]), v.reasons[1], json.loads(v.reasons[2])
        self.assertTrue(set(env_keys) <= {"PATH", "LANG", "HOME", "TMPDIR", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"},
                        env_keys)
        self.assertNotIn("OLLAMA_API_KEY", env_keys)
        self.assertEqual(listing, [])
        self.assertIn("scraper-verifier-", cwd)
        self.assertFalse(Path(cwd).exists())  # removed afterwards

    def test_timeout_kills_the_whole_process_group_and_is_unknown(self):
        pidfile = Path(self.tmp.name) / "grandchild.pid"
        cmd = self.script(f"""
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(pidfile)!r}, "w").write(str(child.pid))
            time.sleep(60)
        """)
        started = time.monotonic()
        v = verify(ExternalV1({"command": cmd, "timeout_seconds": 1}), qa_row())
        self.assertEqual(v, Verdict.unknown("timed out after 1s"))
        self.assertLess(time.monotonic() - started, 15)
        grandchild = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail(f"grandchild {grandchild} survived the group kill")

    def test_oversized_stdout_nonzero_exit_and_garbage_are_unknown(self):
        big = self.script("import sys; sys.stdout.write('x' * 10_000_000)")
        with patch.object(config, "EXTERNAL_VERIFIER_MAX_STDOUT_BYTES", 1000):
            self.assertEqual(verify(ExternalV1({"command": big}), qa_row()),
                             Verdict.unknown("stdout over 1000 bytes"))
        crash = self.script("import sys; sys.stderr.write('boom'); sys.exit(4)")
        self.assertEqual(verify(ExternalV1({"command": crash}), qa_row()), Verdict.unknown("exit code 4: boom"))
        garbage = self.script("print('PASS')")
        self.assertEqual(verify(ExternalV1({"command": garbage}), qa_row()).verdict, "unknown")
        bad = self.script("import json; print(json.dumps({'verdict': 'maybe'}))")
        self.assertEqual(verify(ExternalV1({"command": bad}), qa_row()).verdict, "unknown")
        missing = ExternalV1({"command": [str(Path(self.tmp.name) / "no-such-binary")]})
        self.assertIn("could not start", verify(missing, qa_row()).reasons[0])

    def test_concurrency_is_limited_by_the_setting(self):
        log = Path(self.tmp.name) / "log"
        cmd = self.script(f"""
            import json, os, time
            with open({str(log)!r}, "a") as f: f.write(f"+ {{time.monotonic()}}\\n")
            time.sleep(0.3)
            with open({str(log)!r}, "a") as f: f.write(f"- {{time.monotonic()}}\\n")
            print(json.dumps({{"verdict": "pass", "reasons": []}}))
        """)
        runner = VerifierRunner([(ExternalV1({"command": cmd}), "flag")], external_concurrency=2)
        rows = [qa_row(f"Question number {i}?", chunk=i) for i in range(6)]
        out = run(runner.run("https://s.test/a", rows))
        self.assertEqual([r["verification_status"] for r in out], ["pass"] * 6)
        events = sorted((float(t), 1 if sign == "+" else -1) for sign, t in
                        (line.split() for line in log.read_text().splitlines()))
        level = peak = 0
        for _t, d in events:
            level += d
            peak = max(peak, level)
        self.assertEqual(peak, 2)

    def test_option_validation(self):
        for opts in ({}, {"command": "check.sh"}, {"command": []}, {"command": ["x"], "timeout_seconds": 0},
                     {"command": ["x"], "timeout_seconds": 10_000}, {"command": ["x"], "record_types": "qa"},
                     {"command": ["x"], "shell": True}):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                ExternalV1(opts)
        self.assertFalse(ExternalV1({"command": ["x"], "record_types": ["code_example"]}).applies(qa_row()))


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
class _Fixed(Verifier):
    name, version = "fixed", 1

    def __init__(self, verdict, boom=False):
        self.v, self.boom = verdict, boom

    async def verify(self, row, page):
        if self.boom:
            raise RuntimeError("broken")
        return self.v


class TestRunner(unittest.TestCase):
    def test_drop_flag_unknown_and_statuses(self):
        runner = VerifierRunner([(_Fixed(Verdict.fail("bad")), "flag"), (_Fixed(Verdict.unknown("?")), "drop")])
        (row,) = run(runner.run("u", [qa_row()]))
        self.assertEqual(row["verification_status"], "fail")
        self.assertEqual([r["verdict"] for r in row["verification"]], ["fail", "unknown"])
        runner = VerifierRunner([(_Fixed(Verdict.fail("bad")), "drop")])
        self.assertEqual(run(runner.run("u", [qa_row(), qa_row()])), [])
        rep = runner.report()
        self.assertEqual((rep["rows_checked"], rep["rows_dropped"]), (2, 2))
        self.assertEqual(rep["verifiers"][0]["dropped"], 2)
        runner = VerifierRunner([(_Fixed(Verdict.unknown("?")), "flag"), (_Fixed(Verdict.ok()), "flag")])
        self.assertEqual(run(runner.run("u", [qa_row()]))[0]["verification_status"], "unknown")

    def test_unverified_when_nothing_applies_and_a_crash_is_unknown(self):
        runner = VerifierRunner([(CodeSyntaxV1(), "drop")])
        (row,) = run(runner.run("u", [qa_row()]))
        self.assertEqual((row["verification"], row["verification_status"]), ([], "unverified"))
        runner = VerifierRunner([(_Fixed(None, boom=True), "drop")])
        (row,) = run(runner.run("u", [qa_row()]))
        self.assertEqual(row["verification"][0]["reasons"], ["verifier error: RuntimeError"])

    def test_no_verifiers_leaves_rows_untouched(self):
        rows = [qa_row()]
        self.assertEqual(run(VerifierRunner([]).run("u", rows)), [qa_row()])


class TestConfig(unittest.TestCase):
    def cfg(self, **extra):
        return {"contract_version": 2, "mode": "crawl", "run_dir": "/nonexistent-scraper-test-run-dir/x",
                "seeds": [["https://s.test/", None]], "host_prefix_pairs": [["s.test", "/"]], **extra}

    def test_validation_and_defaults(self):
        self.assertEqual([(v.id, f) for v, f in verifiers.build()], [("schema@1", "flag")])
        self.assertEqual(verifiers.build([]), [])
        eff = headless.validate(self.cfg(verifiers=[
            {"name": "schema", "version": 1, "on_fail": "drop"}, {"name": "near_duplicate", "version": 1},
            {"name": "external", "version": 1, "options": {"command": ["/bin/true"]}}]), environ={}).effective
        self.assertEqual(eff["external_verifier_concurrency"], {"value": 2, "source": "default"})
        for bad, msg in (([{"name": "render", "version": 1}], "unknown verifier"),
                         ([{"name": "schema", "version": 1, "on_fail": "keep"}], "on_fail"),
                         ([{"name": "external", "version": 1}], "options.command"),
                         ([{"name": "schema", "version": 1}] * 2, "listed twice"), ({}, "must be a list")):
            with self.subTest(bad=bad), self.assertRaisesRegex(headless.HeadlessConfigError, msg):
                headless.validate(self.cfg(verifiers=bad), environ={})
        with self.assertRaisesRegex(headless.HeadlessConfigError, ">= 1"):
            headless.validate(self.cfg(settings={"external_verifier_concurrency": 0}), environ={})

    def test_v1_refuses_them(self):
        with self.assertRaisesRegex(headless.HeadlessConfigError, "unknown config keys: verifiers"):
            headless.validate({**self.cfg(), "contract_version": 1, "verifiers": []}, environ={})


# ---------------------------------------------------------------------------
# whole v2 runs
# ---------------------------------------------------------------------------
class _Crawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


PAGES = {"/": ("# Home\n\n" + "Intro prose about the tool. " * 60 + "\n\nRun it:\n\n```bash\nuv run app\n```\n\n"
               + "Closing prose. " * 60, [])}


def _site(crawler):
    async def fetch(url):
        text, links = PAGES["/"]
        return text, links
    return fetch


async def _robots(url):
    return (200, "User-agent: *\nAllow: /\n") if url.endswith("/robots.txt") else (404, None)


class TestVerifiersInARun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(config, "DEFAULT_POLITENESS_DELAY_SECONDS", 0.0)
        p.start()
        self.addCleanup(p.stop)

    async def llm_call(self, system, text):
        if system == CodeExampleV1.system_prompt:
            return json.dumps([
                {"instruction": "Run the app", "code": "uv run app", "language": "bash", "explanation": "Runs."},
                {"instruction": "Deploy it", "code": "kubectl apply -f prod.yaml", "language": "bash",
                 "explanation": "Invented."}])
        return json.dumps([{"instruction": "What is the tool?", "response": "A crawler that builds datasets."},
                           {"instruction": "What is the tool", "response": "A crawler that builds datasets!"},
                           {"instruction": "Short?", "response": "Too short."}])

    def test_drop_and_flag_reach_the_written_rows_and_result_json(self):
        run_dir = Path(self.tmp.name) / "run"
        run_dir.mkdir()
        cfg = {"contract_version": 2, "mode": "crawl", "run_dir": str(run_dir),
               "seeds": [[f"http://{HOST}/", None]], "host_prefix_pairs": [[HOST, "/"]],
               "provider": "ollama_cloud", "model": "m", "max_pages": 1, "crawl_workers": 1, "extract_workers": 1,
               "intent": "", "generators": [{"name": "qa", "version": 1}, {"name": "code_example", "version": 1}],
               "verifiers": [{"name": "schema", "version": 1, "on_fail": "drop"},
                             {"name": "near_duplicate", "version": 1, "on_fail": "flag"},
                             {"name": "code_syntax", "version": 1, "on_fail": "flag"}],
               "settings": {"llm_breaker_threshold": 0, "provider_preflight": False, "max_rows_per_page": 1000}}

        async def runner(c, api_key=None, context=None):
            return await main.headless_crawl(c, api_key=api_key, context=context, _crawler_factory=_Crawler,
                                             _fetch_fn_factory=_site, _llm_call=self.llm_call, _http_fetch=_robots,
                                             _llm=object())

        code = main.headless_v1_entry(cfg, run=runner, out=io.StringIO(), err=io.StringIO(),
                                      environ={"OLLAMA_API_KEY": "dummy"})
        self.assertEqual(code, 0)
        result = json.loads((run_dir / "result.json").read_text())
        rows = [json.loads(line) for line in (run_dir / "canonical.jsonl").read_text().splitlines()]
        self.assertFalse(any(r.get("code") == "kubectl apply -f prod.yaml" for r in rows))  # hallucinated: dropped
        self.assertFalse(any(r.get("answer") == "Too short." for r in rows))                # too short: dropped
        code_rows = [r for r in rows if r["record_type"] == "code_example"]
        self.assertEqual([r["code"] for r in code_rows], ["uv run app"])
        self.assertEqual([v["verifier"] for v in code_rows[0]["verification"]],
                         ["schema", "near_duplicate", "code_syntax"])
        self.assertEqual(code_rows[0]["verification_status"], "unknown")  # no bash parser
        statuses = [r["verification_status"] for r in rows if r["record_type"] == "qa"]
        self.assertIn("fail", statuses)   # near-duplicate questions are flagged, not dropped
        ver = result["verification"]
        schema = ver["verifiers"][0]
        self.assertEqual((schema["verifier"], schema["on_fail"]), ("schema", "drop"))
        self.assertEqual(schema["dropped"], ver["rows_dropped"])
        self.assertEqual(ver["rows_checked"] - ver["rows_dropped"], len(rows))
        self.assertEqual(sum(ver["written_rows_by_status"].values()), len(rows))
        self.assertEqual(result["balance"]["rows_kept"], len(rows))  # caps counted only survivors


if __name__ == "__main__":
    unittest.main()
