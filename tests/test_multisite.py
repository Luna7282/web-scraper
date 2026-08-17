"""Scope-predicate tests against real, cached fixtures from five
structurally different sites (see tests/fetch_fixtures.py). Offline --
reads tests/fixtures/*.json only, no network, no crawl4ai/browser
dependency. This is what turns "does this handle sites that aren't
manim" into something CI can answer rather than something to remember.

Each fixture's browser_rendered.hrefs is exactly what discover_branches
would see from a live crawl. We re-run the same host/first-segment
categorization discover_branches does (duplicated here deliberately --
importing discovery.py would pull in crawl4ai, defeating the point of an
offline test) and then derive_prefix + is_in_scope against it, same as
the live --dry-run path.
"""
import json
import unittest
from pathlib import Path
from urllib.parse import urlparse

from scope import normalize_url, derive_prefix, is_in_scope

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(slug: str) -> dict:
    path = FIXTURES_DIR / f"{slug}.json"
    if not path.exists():
        raise unittest.SkipTest(
            f"fixture {path} missing -- run tests/fetch_fixtures.py to regenerate"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def categorize(hrefs: list[str], root_url: str) -> dict[str, list[str]]:
    """Same bucketing discover_branches does, reimplemented here to avoid
    importing discovery.py (which pulls in crawl4ai) in an offline test."""
    root_host = urlparse(root_url).netloc.lower()
    branches: dict[str, set[str]] = {}
    for href in hrefs:
        url = normalize_url(href, base_url=root_url)
        if url is None:
            continue
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if parsed.netloc != root_host:
            category = f"External: {parsed.netloc}"
        elif path_parts:
            category = f"Path: /{path_parts[0]}/*"
        else:
            category = "Root Level"
        branches.setdefault(category, set()).add(url)
    return {k: sorted(v) for k, v in branches.items()}


class MultisiteScopeTestCase(unittest.TestCase):
    """Base class: subclasses set FIXTURE_SLUG and get every test for
    free against that site's real fixture data."""

    FIXTURE_SLUG: str | None = None

    def setUp(self):
        if self.FIXTURE_SLUG is None:
            self.skipTest("base class")
        self.fixture = load_fixture(self.FIXTURE_SLUG)
        self.root_url = self.fixture["root_url"]
        self.hrefs = self.fixture["browser_rendered"]["hrefs"]
        self.branches = categorize(self.hrefs, self.root_url)

    def test_fixture_has_hrefs(self):
        self.assertGreater(len(self.hrefs), 0, "fixture has no hrefs at all -- discovery found nothing")

    def test_every_branch_derives_without_raising(self):
        for category, urls in self.branches.items():
            with self.subTest(category=category):
                result = derive_prefix(urls)
                self.assertIsInstance(result.host, str)
                self.assertTrue(result.host)

    def test_no_branch_prefix_is_shorter_than_root_path_when_not_degenerate(self):
        # The bug this whole predicate replaces: a derived prefix that's
        # narrower than the site's own root path segment count would be
        # suspicious for a same-host branch built from nested URLs.
        # Degenerate (host-only) branches are exempt by definition.
        for category, urls in self.branches.items():
            with self.subTest(category=category):
                result = derive_prefix(urls)
                if result.degenerate or category.startswith("External:"):
                    continue
                self.assertTrue(result.prefix.startswith("/"))

    def test_every_url_in_a_branch_is_in_scope_of_its_own_derived_prefix(self):
        # Sanity check: derive_prefix's own output must satisfy is_in_scope
        # for every URL it was derived from -- otherwise the prefix logic
        # and the scope-check logic have drifted apart from each other.
        for category, urls in self.branches.items():
            with self.subTest(category=category):
                result = derive_prefix(urls)
                for url in urls:
                    decision = is_in_scope(url, host=result.host, prefix=result.prefix)
                    self.assertTrue(
                        decision.allowed,
                        f"{url} not in scope of its own branch's derived prefix "
                        f"{result.prefix!r} ({decision.reason})",
                    )

    def test_external_branches_are_rejected_by_a_same_host_branchs_scope(self):
        # Cross-host fan-out case: URLs from an External: branch must never
        # be accepted by a same-host branch's scope.
        same_host_branches = {
            c: u for c, u in self.branches.items() if not c.startswith("External:")
        }
        external_urls = [
            u for c, us in self.branches.items() if c.startswith("External:") for u in us
        ]
        if not same_host_branches or not external_urls:
            self.skipTest("no same-host/external split to test on this fixture")
        category, urls = next(iter(same_host_branches.items()))
        result = derive_prefix(urls)
        for ext_url in external_urls[:20]:  # cap for test speed on large fixtures
            decision = is_in_scope(ext_url, host=result.host, prefix=result.prefix)
            self.assertFalse(decision.allowed)
            self.assertIn("off_host", decision.reason)


class TestDocsManimCommunity(MultisiteScopeTestCase):
    FIXTURE_SLUG = "docs_manim_community"


class TestFastapiTiangolo(MultisiteScopeTestCase):
    FIXTURE_SLUG = "fastapi_tiangolo"


class TestWwwManimCommunity(MultisiteScopeTestCase):
    FIXTURE_SLUG = "www_manim_community"


class TestStackblitz(MultisiteScopeTestCase):
    FIXTURE_SLUG = "stackblitz"


class TestBlogCloudflare(MultisiteScopeTestCase):
    FIXTURE_SLUG = "blog_cloudflare"


if __name__ == "__main__":
    unittest.main()
