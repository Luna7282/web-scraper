"""decisions/ package: the interface, the base class's default batching,
LLMTraditionalBackend, JevBackend, LayaBackend, and get_backend()'s
dispatch -- all offline, all against stubs. No TYPESAFE_API_KEY, no
laya package, no live network call anywhere in this file."""
import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

import config
from decisions.backend import DecisionBackend, DecisionError
from decisions.dispatch import get_backend
from decisions.jev import JevBackend
from decisions.laya import LayaBackend
from decisions.llm_traditional import LLMTraditionalBackend
from decisions.types import NoulAnswer
from crawl.pipeline import RateLimitError


class _StubBackend(DecisionBackend):
    """Exercises the base class's default noul_many (concurrent gather)
    without overriding it -- proves the default batching behavior."""
    def __init__(self):
        self.calls = []

    async def noul(self, state, instructions, criteria=None):
        self.calls.append((state, instructions, criteria))
        return NoulAnswer(value=1.0 if "yes" in state else 0.0, method="stub")


class TestDecisionBackendDefaultBatching(unittest.TestCase):
    def test_noul_many_default_gathers_individual_calls(self):
        backend = _StubBackend()
        items = {
            "a": ("yes please", "is it?", None),
            "b": ("no thanks", "is it?", None),
        }
        result = asyncio.run(backend.noul_many(items))
        self.assertEqual(result["a"].value, 1.0)
        self.assertEqual(result["b"].value, 0.0)
        self.assertEqual(len(backend.calls), 2)

    def test_choice_and_score_default_to_not_implemented(self):
        backend = _StubBackend()
        with self.assertRaises(NotImplementedError):
            asyncio.run(backend.choice("s", "i", {"a": "x"}))
        with self.assertRaises(NotImplementedError):
            asyncio.run(backend.score("s", "i", ["a", "b"]))


class TestLLMTraditionalBackend(unittest.TestCase):
    def test_noul_many_parses_batched_response(self):
        async def chat_fn(system_prompt, user_prompt):
            return json.dumps([
                {"key": "p1", "answer": True, "reason": "well-formed and accurate"},
                {"key": "p2", "answer": False, "reason": "answer doesn't match the source"},
            ])

        backend = LLMTraditionalBackend(chat_fn, system_prompt="evaluate quality")
        items = {
            "p1": ("Q: X\nA: Y", "Is this a good pair?", None),
            "p2": ("Q: X\nA: Z", "Is this a good pair?", None),
        }
        result = asyncio.run(backend.noul_many(items))
        self.assertEqual(result["p1"].value, 1.0)
        self.assertEqual(result["p1"].method, "traditional")
        self.assertEqual(result["p1"].reason, "well-formed and accurate")
        self.assertEqual(result["p2"].value, 0.0)

    def test_noul_many_salvages_fenced_response(self):
        async def chat_fn(system_prompt, user_prompt):
            return "```json\n" + json.dumps([{"key": "p1", "answer": None, "reason": "unclear"}]) + "\n```"

        backend = LLMTraditionalBackend(chat_fn, system_prompt="x")
        result = asyncio.run(backend.noul_many({"p1": ("s", "i", None)}))
        self.assertEqual(result["p1"].value, 0.5)

    def test_missing_item_in_response_falls_back_not_dropped(self):
        async def chat_fn(system_prompt, user_prompt):
            return json.dumps([{"key": "p1", "answer": True, "reason": "ok"}])

        backend = LLMTraditionalBackend(chat_fn, system_prompt="x")
        result = asyncio.run(backend.noul_many({
            "p1": ("s", "i", None), "p2": ("s", "i", None),
        }))
        self.assertEqual(len(result), 2)
        self.assertEqual(result["p2"].value, 0.5)
        self.assertIn("did not return", result["p2"].reason)

    def test_unparseable_response_raises_decision_error(self):
        async def chat_fn(system_prompt, user_prompt):
            return "not json at all {{{"

        backend = LLMTraditionalBackend(chat_fn, system_prompt="x")
        with self.assertRaises(DecisionError):
            asyncio.run(backend.noul_many({"p1": ("s", "i", None)}))

    def test_single_item_noul_convenience_method(self):
        async def chat_fn(system_prompt, user_prompt):
            return json.dumps([{"key": "_", "answer": True, "reason": "yes"}])

        backend = LLMTraditionalBackend(chat_fn, system_prompt="x")
        answer = asyncio.run(backend.noul("state", "question?"))
        self.assertEqual(answer.value, 1.0)


class TestJevBackend(unittest.TestCase):
    def _mock_response(self, status_code, json_body=None, text=""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_body or {}
        resp.text = text
        return resp

    def test_noul_success(self):
        backend = JevBackend(api_key="sk-test")
        body = {
            "model": "jev-1.0", "answers": {"_": {"type": "noul", "noul": 0.87}}, "usage": {},
        }
        with patch("decisions.jev.requests.post", return_value=self._mock_response(200, body)) as mock_post:
            answer = asyncio.run(backend.noul("some state", "is this relevant?"))
        self.assertEqual(answer.value, 0.87)
        self.assertEqual(answer.method, "jev")
        # Confirms the exact documented request shape, not just "it was called"
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["state"], "some state")
        self.assertEqual(sent["model"], "jev-latest")
        self.assertEqual(sent["questions"]["_"]["type"], "noul")
        self.assertEqual(sent["questions"]["_"]["instructions"], "is this relevant?")
        self.assertEqual(mock_post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")

    def test_401_raises_decision_error(self):
        backend = JevBackend(api_key="bad-key")
        with patch("decisions.jev.requests.post", return_value=self._mock_response(401)):
            with self.assertRaises(DecisionError):
                asyncio.run(backend.noul("s", "i"))

    def test_422_raises_decision_error(self):
        backend = JevBackend(api_key="sk-test")
        with patch("decisions.jev.requests.post", return_value=self._mock_response(422, text="bad schema")):
            with self.assertRaises(DecisionError):
                asyncio.run(backend.noul("s", "i"))

    def test_429_raises_rate_limit_error_not_decision_error(self):
        # Must reuse the pipeline's existing RateLimitError -- worker loops
        # already know how to handle it (release the row, backoff, move on).
        backend = JevBackend(api_key="sk-test")
        with patch("decisions.jev.requests.post", return_value=self._mock_response(429)):
            with self.assertRaises(RateLimitError):
                asyncio.run(backend.noul("s", "i"))

    def test_529_overloaded_also_raises_rate_limit_error(self):
        backend = JevBackend(api_key="sk-test")
        with patch("decisions.jev.requests.post", return_value=self._mock_response(529)):
            with self.assertRaises(RateLimitError):
                asyncio.run(backend.noul("s", "i"))

    def test_missing_api_key_raises_immediately(self):
        with self.assertRaises(DecisionError):
            JevBackend(api_key=None)


class TestLayaBackend(unittest.TestCase):
    def test_construction_without_package_raises_decision_error(self):
        # laya IS installed in this environment now (approved and
        # installed for real -- see LESSONS_LEARNED.md), so the absence
        # case has to be simulated rather than relied on as ambient
        # state -- sys.modules["laya"] = None is the standard way to make
        # `import laya` raise ImportError regardless of what's actually
        # on disk, so this test stays meaningful either way.
        with patch.dict("sys.modules", {"laya": None}):
            with self.assertRaises(DecisionError):
                LayaBackend()


class TestGetBackendDispatch(unittest.TestCase):
    def test_disabled_returns_traditional_unchanged(self):
        traditional = _StubBackend()
        with patch.object(config, "USE_SYSTEM_ONE", False):
            self.assertIs(get_backend(traditional), traditional)

    def test_enabled_jev_returns_jev_backend_not_traditional(self):
        traditional = _StubBackend()
        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch.object(config, "SYSTEM_ONE_PROVIDER", "jev"), \
             patch.dict("os.environ", {"TYPESAFE_API_KEY": "sk-test"}):
            import decisions.dispatch as dispatch_module
            dispatch_module._jev_singleton = None
            backend = get_backend(traditional)
        self.assertIsInstance(backend, JevBackend)
        self.assertIsNot(backend, traditional)

    def test_jev_backend_is_a_singleton_across_calls(self):
        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch.object(config, "SYSTEM_ONE_PROVIDER", "jev"), \
             patch.dict("os.environ", {"TYPESAFE_API_KEY": "sk-test"}):
            import decisions.dispatch as dispatch_module
            dispatch_module._jev_singleton = None
            first = get_backend(_StubBackend())
            second = get_backend(_StubBackend())
        self.assertIs(first, second)  # one call, kept resident -- not reconstructed per decision point


if __name__ == "__main__":
    unittest.main()
