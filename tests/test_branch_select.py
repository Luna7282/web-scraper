"""crawl/branch_select.py: pure branch extraction from a path_tree
projection, prompt building, response parsing, and the async orchestration
-- all offline, with a stub classify_fn standing in for the real LLM call
(same injected-dependency pattern as extract_fn/score_fn elsewhere)."""
import asyncio
import json
import unittest
from unittest.mock import patch

import config
from decisions.types import NoulAnswer
from crawl.branch_select import (
    MalformedClassificationError,
    build_classification_prompt,
    extract_branches,
    parse_branch_classification,
    suggest_branch_selection,
)


def _url_node(url: str) -> dict:
    return {"id": url, "url": url, "status": "done", "depth": 0, "relevance_score": None,
            "host": "x.test", "cross_host": False, "inbound_count": 0}


def _node(segment: str, path: str, children: dict | None = None, url: str | None = None) -> dict:
    return {
        "segment": segment, "path": path,
        "url_node": _url_node(url) if url else None,
        "children": children or {},
    }


# https://docs.manim.community/en/stable/{reference,changelog}/... shape --
# two passthrough segments (en, stable) before the real fork.
DOCS_TREE = _node("", "/", {
    "en": _node("en", "/en", {
        "stable": _node("stable", "/en/stable", {
            "reference": _node("reference", "/en/stable/reference", {
                "VGroup.html": _node("VGroup.html", "/en/stable/reference/VGroup.html",
                                      url="https://docs.manim.community/en/stable/reference/VGroup.html"),
                "Cube.html": _node("Cube.html", "/en/stable/reference/Cube.html",
                                    url="https://docs.manim.community/en/stable/reference/Cube.html"),
            }),
            "changelog": _node("changelog", "/en/stable/changelog", {
                f"0.{i}-changelog.html": _node(
                    f"0.{i}-changelog.html", f"/en/stable/changelog/0.{i}-changelog.html",
                    url=f"https://docs.manim.community/en/stable/changelog/0.{i}-changelog.html",
                )
                for i in range(3)
            }),
        }),
    }),
})

# A flat blog: no passthrough, root forks immediately into many
# single-page "branches".
BLOG_TREE = _node("", "/", {
    f"post-{i}": _node(f"post-{i}", f"/post-{i}", url=f"https://blog.test/post-{i}")
    for i in range(3)
})


class TestExtractBranches(unittest.TestCase):
    def test_skips_passthrough_segments_to_the_real_fork(self):
        branches = extract_branches(DOCS_TREE)
        prefixes = {b["prefix"] for b in branches}
        self.assertEqual(prefixes, {"/en/stable/reference", "/en/stable/changelog"})

    def test_page_count_counts_all_descendants(self):
        branches = {b["prefix"]: b for b in extract_branches(DOCS_TREE)}
        self.assertEqual(branches["/en/stable/reference"]["page_count"], 2)
        self.assertEqual(branches["/en/stable/changelog"]["page_count"], 3)

    def test_sample_urls_capped_and_sorted(self):
        branches = {b["prefix"]: b for b in extract_branches(DOCS_TREE, max_samples=2)}
        samples = branches["/en/stable/changelog"]["sample_urls"]
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples, sorted(samples))

    def test_flat_site_with_no_passthrough_forks_at_root(self):
        branches = extract_branches(BLOG_TREE)
        self.assertEqual({b["prefix"] for b in branches}, {"/post-0", "/post-1", "/post-2"})
        self.assertTrue(all(b["page_count"] == 1 for b in branches))

    def test_empty_tree_no_branches(self):
        self.assertEqual(extract_branches(_node("", "/")), [])


class TestBuildClassificationPrompt(unittest.TestCase):
    def test_includes_intent_prefix_count_and_samples(self):
        branches = [{"prefix": "/reference", "page_count": 40, "sample_urls": ["https://x.test/reference/A.html"]}]
        prompt = build_classification_prompt(branches, "how do I use this library")
        self.assertIn("how do I use this library", prompt)
        self.assertIn("/reference", prompt)
        self.assertIn("40", prompt)
        self.assertIn("https://x.test/reference/A.html", prompt)


class TestParseBranchClassification(unittest.TestCase):
    def setUp(self):
        self.branches = [
            {"prefix": "/reference", "page_count": 40, "sample_urls": []},
            {"prefix": "/changelog", "page_count": 33, "sample_urls": []},
        ]

    def test_parses_clean_json(self):
        raw = json.dumps([
            {"prefix": "/reference", "decision": "select", "reason": "API docs"},
            {"prefix": "/changelog", "decision": "reject", "reason": "release notes"},
        ])
        result = parse_branch_classification(raw, self.branches)
        self.assertEqual([r["decision"] for r in result], ["select", "reject"])
        self.assertEqual(result[0]["prefix"], "/reference")  # branches' own order preserved

    def test_salvages_a_fenced_response(self):
        raw = "```json\n" + json.dumps([
            {"prefix": "/reference", "decision": "select", "reason": "x"},
            {"prefix": "/changelog", "decision": "reject", "reason": "y"},
        ]) + "\n```"
        result = parse_branch_classification(raw, self.branches)
        self.assertEqual([r["decision"] for r in result], ["select", "reject"])

    def test_salvages_prose_wrapped_json(self):
        raw = "Here you go:\n" + json.dumps([
            {"prefix": "/reference", "decision": "select", "reason": "x"},
            {"prefix": "/changelog", "decision": "reject", "reason": "y"},
        ]) + "\nHope that helps!"
        result = parse_branch_classification(raw, self.branches)
        self.assertEqual([r["decision"] for r in result], ["select", "reject"])

    def test_missing_branch_falls_back_to_uncertain_not_dropped(self):
        raw = json.dumps([{"prefix": "/reference", "decision": "select", "reason": "x"}])
        result = parse_branch_classification(raw, self.branches)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["prefix"], "/changelog")
        self.assertEqual(result[1]["decision"], "uncertain")

    def test_invalid_decision_value_falls_back_to_uncertain(self):
        raw = json.dumps([
            {"prefix": "/reference", "decision": "maybe", "reason": "x"},
            {"prefix": "/changelog", "decision": "reject", "reason": "y"},
        ])
        result = parse_branch_classification(raw, self.branches)
        self.assertEqual(result[0]["decision"], "uncertain")

    def test_unparseable_response_raises(self):
        with self.assertRaises(MalformedClassificationError):
            parse_branch_classification("not json at all {{{", self.branches)


class TestSuggestBranchSelection(unittest.TestCase):
    def test_full_flow_with_stub_classify_fn(self):
        calls = []

        async def stub_classify_fn(prompt: str) -> str:
            calls.append(prompt)
            return json.dumps([
                {"prefix": "/en/stable/reference", "decision": "select", "reason": "API reference pages"},
                {"prefix": "/en/stable/changelog", "decision": "reject", "reason": "release notes only"},
            ])

        result = asyncio.run(suggest_branch_selection(DOCS_TREE, "how do I use this library", stub_classify_fn))
        self.assertEqual(len(calls), 1)  # one call, regardless of branch count
        decisions = {r["prefix"]: r["decision"] for r in result}
        self.assertEqual(decisions, {"/en/stable/reference": "select", "/en/stable/changelog": "reject"})

    def test_empty_tree_never_calls_classify_fn(self):
        async def stub_classify_fn(prompt: str) -> str:
            raise AssertionError("should not be called with no branches")

        result = asyncio.run(suggest_branch_selection(_node("", "/"), "intent", stub_classify_fn))
        self.assertEqual(result, [])


class TestSuggestBranchSelectionSystemOne(unittest.TestCase):
    """USE_SYSTEM_ONE=true routes through decisions.get_backend() instead
    of classify_fn -- per-branch noul, sample URLs as state, thresholded
    back to select/reject/uncertain."""

    def test_jev_style_backend_routes_per_branch_noul_not_classify_fn(self):
        async def stub_classify_fn(prompt: str) -> str:
            raise AssertionError("traditional classify_fn must not be called when System One is active")

        async def fake_noul_many(items):
            # reference/ gets a high P(relevant), changelog/ a low one --
            # proves per-item state (not a shared prompt) drove the answer.
            return {
                key: NoulAnswer(value=0.9 if "reference" in key else 0.1, method="jev")
                for key in items
            }

        stub_backend = type("StubBackend", (), {"noul_many": staticmethod(fake_noul_many)})()

        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch("crawl.branch_select.get_backend", return_value=stub_backend):
            result = asyncio.run(suggest_branch_selection(DOCS_TREE, "how do I use this library", stub_classify_fn))

        decisions = {r["prefix"]: r["decision"] for r in result}
        self.assertEqual(decisions["/en/stable/reference"], "select")
        self.assertEqual(decisions["/en/stable/changelog"], "reject")
        self.assertEqual(result[0]["decision_method"], "jev")

    def test_jev_style_backend_with_no_reason_gets_an_explicit_placeholder(self):
        async def stub_classify_fn(prompt: str) -> str:
            raise AssertionError("must not be called")

        async def fake_noul_many(items):
            return {key: NoulAnswer(value=0.5, method="jev", reason=None) for key in items}

        stub_backend = type("StubBackend", (), {"noul_many": staticmethod(fake_noul_many)})()

        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch("crawl.branch_select.get_backend", return_value=stub_backend):
            result = asyncio.run(suggest_branch_selection(DOCS_TREE, "intent", stub_classify_fn))

        for r in result:
            self.assertIsNotNone(r["reason"])
            self.assertIn("System One", r["reason"])
            self.assertEqual(r["decision"], "uncertain")  # 0.5 falls in neither threshold band


if __name__ == "__main__":
    unittest.main()
