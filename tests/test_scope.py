"""Unit tests for scope.py -- pure logic, no network.

Covers the cases that actually broke the original scoping bug and its
would-be replacements: nested paths, the degenerate longest-common-path
case, off-host links, protocol-relative/relative hrefs, non-http schemes,
query/fragment normalization, and trailing-slash equivalence.
"""
import unittest

from crawl.scope import normalize_url, derive_prefix, is_in_scope

BASE = "https://docs.manim.community/en/stable/"


class TestNormalizeUrl(unittest.TestCase):
    def test_relative_href_resolved_against_base(self):
        self.assertEqual(
            normalize_url("reference/index.html", BASE),
            "https://docs.manim.community/en/stable/reference/index.html",
        )

    def test_dotdot_relative_href_resolved(self):
        self.assertEqual(
            normalize_url("../latest/index.html", BASE),
            "https://docs.manim.community/en/latest/index.html",
        )

    def test_protocol_relative_href_gets_base_scheme(self):
        self.assertEqual(
            normalize_url("//cdn.example.com/lib.js", BASE),
            "https://cdn.example.com/lib.js",
        )

    def test_off_host_absolute_href_preserved_as_is(self):
        self.assertEqual(
            normalize_url("https://github.com/ManimCommunity/manim", BASE),
            "https://github.com/ManimCommunity/manim",
        )

    def test_mailto_rejected(self):
        self.assertIsNone(normalize_url("mailto:someone@example.com", BASE))

    def test_javascript_scheme_rejected(self):
        self.assertIsNone(normalize_url("javascript:void(0)", BASE))

    def test_tel_scheme_rejected(self):
        self.assertIsNone(normalize_url("tel:+15551234567", BASE))

    def test_empty_href_rejected(self):
        self.assertIsNone(normalize_url("", BASE))

    def test_whitespace_only_href_rejected(self):
        self.assertIsNone(normalize_url("   ", BASE))

    def test_fragment_only_anchor_normalizes_to_base_page(self):
        # A "#section"-only href stays on the same page; after fragment
        # stripping it must equal the base page's own normalized form --
        # no special-casing needed, this falls out of urljoin + stripping.
        self.assertEqual(normalize_url("#installation", BASE), normalize_url(BASE, BASE))

    def test_trailing_slash_pair_normalize_identically(self):
        a = normalize_url("https://docs.manim.community/en/stable/reference", BASE)
        b = normalize_url("https://docs.manim.community/en/stable/reference/", BASE)
        self.assertEqual(a, b)

    def test_bare_root_path_becomes_slash(self):
        self.assertEqual(
            normalize_url("https://docs.manim.community", BASE),
            "https://docs.manim.community/",
        )

    def test_query_params_sorted_not_dropped(self):
        a = normalize_url("https://x.com/page?b=2&a=1", BASE)
        b = normalize_url("https://x.com/page?a=1&b=2", BASE)
        self.assertEqual(a, b)
        self.assertIn("a=1", a)
        self.assertIn("b=2", a)

    def test_distinct_query_values_stay_distinct(self):
        a = normalize_url("https://x.com/page?tab=1", BASE)
        b = normalize_url("https://x.com/page?tab=2", BASE)
        self.assertNotEqual(a, b)

    def test_query_and_fragment_variant_of_same_url_normalize_identically(self):
        a = normalize_url("https://x.com/page?a=1&b=2#section-one", BASE)
        b = normalize_url("https://x.com/page?b=2&a=1#section-two", BASE)
        self.assertEqual(a, b)

    def test_scheme_and_host_lowercased_path_case_preserved(self):
        self.assertEqual(
            normalize_url("HTTPS://Example.COM/SomePath", BASE),
            "https://example.com/SomePath",
        )


class TestDerivePrefix(unittest.TestCase):
    def test_nested_path_case_docs_manim_reference(self):
        urls = [
            "https://docs.manim.community/en/stable/reference/manim.animation.html",
            "https://docs.manim.community/en/stable/reference/manim.mobject.html",
            "https://docs.manim.community/en/stable/reference/manim.scene.html",
        ]
        result = derive_prefix(urls)
        self.assertFalse(result.degenerate)
        self.assertEqual(result.prefix, "/en/stable/reference/")

    def test_nested_path_does_not_collapse_to_first_segment(self):
        # The exact bug this replaces: naive first-segment derivation would
        # give "/en/", over-scoping the whole site. Mixed-depth URLs under
        # /en/stable/ should still land on /en/stable/, not /en/.
        urls = [
            "https://docs.manim.community/en/stable/tutorials/quickstart.html",
            "https://docs.manim.community/en/stable/guides/configuration.html",
        ]
        result = derive_prefix(urls)
        self.assertEqual(result.prefix, "/en/stable/")

    def test_degenerate_lcp_falls_back_to_host_only(self):
        urls = [
            "https://example.com/",
            "https://example.com/about",
            "https://example.com/contact",
        ]
        result = derive_prefix(urls)
        self.assertTrue(result.degenerate)
        self.assertIsNone(result.prefix)
        self.assertEqual(result.host, "example.com")

    def test_single_url_branch_yields_directory_not_exact_leaf(self):
        urls = ["https://docs.manim.community/en/stable/tutorials/quickstart.html"]
        result = derive_prefix(urls)
        self.assertEqual(result.prefix, "/en/stable/tutorials/")

    def test_single_root_url_is_degenerate(self):
        result = derive_prefix(["https://example.com/"])
        self.assertTrue(result.degenerate)
        self.assertIsNone(result.prefix)

    def test_section_index_page_alongside_its_own_subpages_is_not_degenerate(self):
        # Regression: an earlier version took dirname() of every URL
        # before computing the common path, which meant a section's own
        # index URL ("/how-to", no trailing content) forced the whole
        # branch's common path up to "/" -- discovered via
        # fastapi.tiangolo.com's 51-URL /tutorial/* branch collapsing to
        # fully degenerate host-only scope. See LESSONS_LEARNED.md #8.
        urls = [
            "https://fastapi.tiangolo.com/tutorial",
            "https://fastapi.tiangolo.com/tutorial/body",
            "https://fastapi.tiangolo.com/tutorial/security",
        ]
        result = derive_prefix(urls)
        self.assertFalse(result.degenerate)
        self.assertEqual(result.prefix, "/tutorial/")

    def test_empty_list_raises(self):
        with self.assertRaises(ValueError):
            derive_prefix([])


class TestIsInScope(unittest.TestCase):
    def test_bare_section_index_url_matches_its_own_derived_prefix(self):
        # Regression: prefix always ends in "/" ("/how-to/"), but the
        # section's own index URL normalizes to "/how-to" (no trailing
        # slash) -- a naive path.startswith(prefix) rejects it even
        # though it's exactly the branch this prefix was derived from.
        decision = is_in_scope(
            "https://fastapi.tiangolo.com/how-to",
            host="fastapi.tiangolo.com",
            prefix="/how-to/",
        )
        self.assertTrue(decision.allowed)

    def test_sibling_section_sharing_text_prefix_still_rejected(self):
        # The other half of the same fix: without appending "/" before
        # comparing, "/how-toz" would wrongly match prefix "/how-to/" as a
        # plain string prefix. Must still be rejected.
        decision = is_in_scope(
            "https://fastapi.tiangolo.com/how-toz",
            host="fastapi.tiangolo.com",
            prefix="/how-to/",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("outside_prefix", decision.reason)

    def test_within_prefix_accepted(self):
        decision = is_in_scope(
            "https://docs.manim.community/en/stable/reference/manim.scene.html",
            host="docs.manim.community",
            prefix="/en/stable/reference/",
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "accepted")

    def test_outside_prefix_rejected(self):
        decision = is_in_scope(
            "https://docs.manim.community/en/stable/tutorials/quickstart.html",
            host="docs.manim.community",
            prefix="/en/stable/reference/",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("outside_prefix", decision.reason)

    def test_off_host_rejected(self):
        decision = is_in_scope(
            "https://github.com/ManimCommunity/manim",
            host="docs.manim.community",
            prefix=None,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("off_host", decision.reason)

    def test_host_only_prefix_none_accepts_any_path_on_host(self):
        decision = is_in_scope(
            "https://example.com/anything/at/all",
            host="example.com",
            prefix=None,
        )
        self.assertTrue(decision.allowed)

    def test_already_visited_rejected_even_via_differently_ordered_variant(self):
        canonical_a = normalize_url("https://x.com/page?a=1&b=2#one", BASE)
        canonical_b = normalize_url("https://x.com/page?b=2&a=1#two", BASE)
        self.assertEqual(canonical_a, canonical_b)  # precondition for this test

        visited = {canonical_a}
        decision = is_in_scope(canonical_b, host="x.com", prefix=None, visited=visited)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "already_visited")


if __name__ == "__main__":
    unittest.main()
