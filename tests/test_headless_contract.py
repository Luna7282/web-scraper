"""Headless contract v1 (Brief 3A Step 1): versioning, strict keys, settings
precedence, exit codes. Offline; the subprocess tests run the real main.py
but fail at config validation, before any crawl or network.

ConfigGuard: every test here that could touch the config module checks in
tearDown that the v1-settings attributes are exactly as before the test.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import config
import headless
import main

REPO = Path(__file__).resolve().parents[1]
SECRET = "s" + "k-" + "abcdefghijklmnopqrstuvwxyz012345"  # assembled at runtime: no key-shaped literal


def v1(**overrides):
    cfg = {"contract_version": 1, "mode": "crawl", "run_dir": "/tmp/x",
           "seeds": [["https://example.test/", None]], "host_prefix_pairs": [["example.test", "/"]]}
    cfg.update(overrides)
    return cfg


class ConfigGuard(unittest.TestCase):
    def setUp(self):
        self._snapshot = headless.settings_snapshot()

    def tearDown(self):
        self.assertEqual(headless.settings_snapshot(), self._snapshot,
                         "config module differs after the test: v1 settings leaked")


class TestVersioning(ConfigGuard):
    def test_no_version_means_legacy(self):
        self.assertFalse(headless.is_versioned({"mode": "crawl", "root_url": "x"}))
        self.assertTrue(headless.is_versioned(v1()))

    def test_supported_and_unsupported_versions(self):
        self.assertEqual(headless.validate(v1(), {}).contract_version, 1)
        for bad in (2, 0, "1", True, 1.0, None):
            with self.subTest(version=bad), self.assertRaises(headless.HeadlessConfigError) as ctx:
                headless.validate(v1(contract_version=bad), {})
            self.assertIn("supports [1]", str(ctx.exception))


class TestStrictKeys(ConfigGuard):
    def test_a_typo_key_is_refused_by_name(self):
        with self.assertRaises(headless.HeadlessConfigError) as ctx:
            headless.validate(v1(max_pagez=5), {})
        self.assertEqual(str(ctx.exception), "unknown config keys: max_pagez")

    def test_all_unknown_keys_are_listed_sorted(self):
        with self.assertRaises(headless.HeadlessConfigError) as ctx:
            headless.validate(v1(zeta=1, root_url="x", alpha=2), {})
        self.assertEqual(str(ctx.exception), "unknown config keys: alpha, root_url, zeta")

    def test_every_key_today_and_every_v1_key_is_accepted(self):
        cfg = v1(intent="", provider="gemini", model="m", max_pages=5, max_depth=0, crawl_workers=2,
                 extract_workers=2, extract_threshold=0.0, follow_threshold=0.0, build_rag=False,
                 enable_quality_scoring=False, quality_threshold=None, run_id="r-1", resume=False, settings={})
        self.assertEqual(headless.validate(cfg, {}).cfg, cfg)

    def test_shape_errors(self):
        cases = [
            ({"mode": "bogus"}, "mode must be one of"),
            ({"run_dir": ""}, "run_dir"),
            ({"seeds": []}, "seeds"),
            ({"seeds": ["https://x.test/"]}, "seeds"),
            ({"host_prefix_pairs": [["h"]]}, "host_prefix_pairs"),
            ({"resume": "yes"}, "resume"),
            ({"run_id": "../escape"}, "run_id"),
            ({"api_key_env": "not a name"}, "api_key_env"),
        ]
        for overrides, fragment in cases:
            with self.subTest(overrides=overrides), self.assertRaises(headless.HeadlessConfigError) as ctx:
                headless.validate(v1(**overrides), {})
            self.assertIn(fragment, str(ctx.exception))
        cfg = v1()
        del cfg["run_dir"]
        with self.assertRaises(headless.HeadlessConfigError) as ctx:
            headless.validate(cfg, {})
        self.assertIn("missing required config keys: run_dir", str(ctx.exception))

    def test_api_key_env_must_be_set_and_its_value_is_never_in_an_error(self):
        with self.assertRaises(headless.HeadlessConfigError) as ctx:
            headless.validate(v1(api_key_env="FOUNDRY_RUN_KEY"), {"FOUNDRY_RUN_KEY": "   "})
        self.assertIn("FOUNDRY_RUN_KEY", str(ctx.exception))
        hr = headless.validate(v1(api_key_env="FOUNDRY_RUN_KEY"), {"FOUNDRY_RUN_KEY": SECRET})
        self.assertEqual(hr.api_key_env, "FOUNDRY_RUN_KEY")
        self.assertNotIn(SECRET, repr(hr))


class TestPrecedence(ConfigGuard):
    def test_config_then_env_then_default_with_sources(self):
        env = {"EXTRACT_UNIT_CONCURRENCY": "3", "LLM_BREAKER_THRESHOLD": "7", "MAX_PAGES_COUNTS_FAILED": "TRUE"}
        eff = headless.validate(v1(settings={"extract_unit_concurrency": 4}), env).effective
        self.assertEqual(eff["extract_unit_concurrency"], {"value": 4, "source": "config"})  # config beats env
        self.assertEqual(eff["llm_breaker_threshold"], {"value": 7, "source": "env"})
        self.assertEqual(eff["max_pages_counts_failed"], {"value": True, "source": "env"})  # same parsing as config.py
        self.assertEqual(eff["llm_sdk_max_retries"], {"value": 2, "source": "default"})
        self.assertEqual(eff["system_one_provider"], {"value": None, "source": "default"})
        self.assertEqual(set(eff), set(headless.SETTINGS))

    def test_v1_defaults_equal_todays_config_defaults_except_step_4s_safe_defaults(self):
        """v1 defaults match config.py except the four Step 4 safe defaults,
        which differ on purpose (v1 headless only)."""
        todays = {"EXTRACT_UNIT_CONCURRENCY": 1, "MAX_PAGES_COUNTS_FAILED": False, "LLM_BREAKER_THRESHOLD": 0,
                  "LLM_SDK_MAX_RETRIES": 2, "USE_SYSTEM_ONE": False, "SYSTEM_ONE_PROVIDER": None,
                  "EXTRACT_TIMING_LOG": False, "STOP_GRACE_SECONDS": 30, "SITE_RETRY_BACKOFF": False,
                  "LLM_UNAVAILABLE_MAX_RELEASES": 0}
        step_4 = {"MAX_PAGES_COUNTS_FAILED": True, "LLM_BREAKER_THRESHOLD": 5, "SITE_RETRY_BACKOFF": True,
                  "LLM_UNAVAILABLE_MAX_RELEASES": 5}
        for name, spec in headless.SETTINGS.items():
            self.assertEqual(spec.default, {**todays, **step_4}[spec.config_attr], name)

    def test_bad_settings_are_refused(self):
        cases = [({"nope": 1}, "unknown settings: nope"),
                 ({"extract_unit_concurrency": "4"}, "must be an integer"),
                 ({"extract_unit_concurrency": True}, "must be an integer"),  # bool is not an int here
                 ({"max_pages_counts_failed": 1}, "true or false"),
                 ({"extract_unit_concurrency": 0}, ">= 1"),
                 ({"llm_breaker_threshold": -1}, ">= 0")]
        for settings, fragment in cases:
            with self.subTest(settings=settings), self.assertRaises(headless.HeadlessConfigError) as ctx:
                headless.validate(v1(settings=settings), {})
            self.assertIn(fragment, str(ctx.exception))
        with self.assertRaises(headless.HeadlessConfigError) as ctx:
            headless.validate(v1(), {"EXTRACT_UNIT_CONCURRENCY": "four"})
        self.assertIn("EXTRACT_UNIT_CONCURRENCY='four'", str(ctx.exception))

    def test_applied_settings_set_then_restore_even_on_error(self):
        eff = headless.validate(v1(settings={"extract_unit_concurrency": 6, "llm_breaker_threshold": 4}), {}).effective
        before = headless.settings_snapshot()
        with self.assertRaises(RuntimeError), headless.applied_settings(eff):
            self.assertEqual((config.EXTRACT_UNIT_CONCURRENCY, config.LLM_BREAKER_THRESHOLD), (6, 4))
            self.assertEqual(config.effective_llm_sdk_max_retries(), 0)  # readers see the applied values
            raise RuntimeError("mid-run failure")
        self.assertEqual(headless.settings_snapshot(), before)


class TestV1EntryExitCodes(ConfigGuard):
    def entry(self, cfg, run, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = main.headless_v1_entry(cfg, run=run, out=out, err=err, environ=environ or {})
        return code, out.getvalue(), err.getvalue()

    def test_ok_stopped_early_interrupted(self):
        seen = {}

        async def ok(cfg, api_key=None, context=None):
            seen["concurrency"] = config.EXTRACT_UNIT_CONCURRENCY  # the run sees the applied settings
            return {"mode": "crawl", "counts_by_status": {"done": 2}, "fatal_errors": []}

        async def early(cfg, api_key=None, context=None):
            return {"mode": "crawl", "counts_by_status": {}, "fatal_errors": ["[FATAL] breaker stop"]}

        async def interrupted(cfg, api_key=None, context=None):
            raise KeyboardInterrupt

        code, out, _ = self.entry(v1(settings={"extract_unit_concurrency": 4}), ok)
        self.assertEqual((code, seen["concurrency"]), (headless.EXIT_OK, 4))
        self.assertEqual(json.loads(out)["counts_by_status"], {"done": 2})
        self.assertEqual(self.entry(v1(), early)[0], headless.EXIT_STOPPED_EARLY)
        self.assertEqual(self.entry(v1(), interrupted)[0], headless.EXIT_INTERRUPTED)

    def test_config_errors_exit_2_with_one_line_and_never_run(self):
        async def must_not_run(cfg, api_key=None, context=None):
            raise AssertionError("ran despite a config error")

        code, out, err = self.entry(v1(max_pagez=5), must_not_run)
        self.assertEqual((code, out), (headless.EXIT_CONFIG_ERROR, ""))
        self.assertEqual(err, "config error: unknown config keys: max_pagez\n")
        code, _, err = self.entry(v1(settings={"use_system_one": True}), must_not_run)  # no provider set
        self.assertEqual(code, headless.EXIT_CONFIG_ERROR)
        self.assertTrue(err.startswith("config error: "))
        self.assertEqual(err.count("\n"), 1)

    def test_api_key_reaches_the_run_and_is_never_printed(self):
        seen = {}

        async def run(cfg, api_key=None, context=None):
            seen["api_key"] = api_key
            return {"mode": "crawl", "counts_by_status": {}, "fatal_errors": []}

        code, out, err = self.entry(v1(api_key_env="FOUNDRY_RUN_KEY"), run, environ={"FOUNDRY_RUN_KEY": SECRET})
        self.assertEqual((code, seen["api_key"]), (0, SECRET))
        self.assertNotIn(SECRET, out + err)

    def test_get_llm_uses_an_explicit_key(self):
        from llm_factory import get_llm
        llm, _ = get_llm("ollama_cloud", "m", api_key=SECRET)
        self.assertEqual(llm.openai_api_key.get_secret_value(), SECRET)


class TestRealProcess(unittest.TestCase):
    """The real main.py, stopped at config validation (no crawl, no network)."""

    def run_main(self, cfg):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text(json.dumps(cfg))
            return subprocess.run([sys.executable, "main.py", "--headless-config", str(path)], cwd=REPO,
                                  capture_output=True, text=True, timeout=120, check=False,
                                  env={**os.environ, "ANONYMIZED_TELEMETRY": "False"})

    def test_v1_config_error_exits_2_without_a_traceback(self):
        done = self.run_main(v1(max_pagez=5))
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stderr.strip().splitlines()[-1], "config error: unknown config keys: max_pagez")
        self.assertNotIn("Traceback", done.stderr + done.stdout)
        self.assertEqual(self.run_main(v1(contract_version=2)).returncode, 2)

    def test_legacy_config_is_unchanged(self):
        """No contract_version: today's behaviour -- an extra key is ignored
        and a bad mode is today's ValueError traceback with exit 1."""
        done = self.run_main({"mode": "bogus", "root_url": "x", "run_dir": "/tmp/x",
                              "seeds": [], "host_prefix_pairs": []})
        self.assertEqual(done.returncode, 1)
        self.assertIn("Traceback", done.stderr)
        self.assertIn("unknown headless mode: 'bogus'", done.stderr)


if __name__ == "__main__":
    unittest.main()
