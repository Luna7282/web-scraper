"""Offline tests for sectioning.py -- pure string/URL logic, no I/O."""
import unittest

from content.sectioning import (
    FALLBACK_SECTION,
    cap_slug,
    derive_section,
    disambiguate_slugs,
    sanitize_filename_component,
    slugify,
)


class TestDeriveSection(unittest.TestCase):
    def test_default_depth_two(self):
        self.assertEqual(derive_section("https://x.com/tutorial/security/oauth2"), "tutorial/security")

    def test_configurable_depth(self):
        self.assertEqual(derive_section("https://x.com/a/b/c/d", depth=3), "a/b/c")

    def test_shallow_path_shorter_than_depth(self):
        self.assertEqual(derive_section("https://x.com/tutorial"), "tutorial")

    def test_root_path_uses_fallback(self):
        self.assertEqual(derive_section("https://x.com/"), FALLBACK_SECTION)
        self.assertEqual(derive_section("https://x.com"), FALLBACK_SECTION)


class TestDeriveSectionSeedRelative(unittest.TestCase):
    """step 8 Phase 2A: depth-from-root produced "en/stable" as literally
    every page's section on a real crawl seeded at /en/stable/ -- the
    locale+version segments ate the whole depth budget before any real
    category was reached. Confirmed against the real 1144-row corpus
    before fixing (LESSONS_LEARNED.md)."""

    def test_depth_counted_from_seed_prefix_not_domain_root(self):
        seeds = [("docs.example.com", "/en/stable/")]
        self.assertEqual(
            derive_section("https://docs.example.com/en/stable/reference/x.html", depth=1, seed_prefixes=seeds),
            "reference",
        )

    def test_bare_seed_prefix_url_itself(self):
        seeds = [("docs.example.com", "/en/stable/")]
        self.assertEqual(
            derive_section("https://docs.example.com/en/stable/conduct.html", depth=1, seed_prefixes=seeds),
            "conduct.html",
        )

    def test_no_seed_prefixes_falls_back_to_root_relative(self):
        # Existing callers (tests, or a URL from before this parameter
        # existed) get the original behavior, not an error.
        self.assertEqual(
            derive_section("https://x.com/en/stable/reference/x.html", depth=1, seed_prefixes=None),
            "en",
        )

    def test_multiple_seeds_different_prefixes_longest_match_wins(self):
        # A crawl with two selected branches on the same host, one
        # nested inside the other -- the more specific prefix should win
        # rather than either one winning arbitrarily by list order.
        seeds = [
            ("docs.example.com", "/en/"),
            ("docs.example.com", "/en/stable/reference/"),
        ]
        self.assertEqual(
            derive_section("https://docs.example.com/en/stable/reference/x.html", depth=1, seed_prefixes=seeds),
            "x.html",
        )
        self.assertEqual(
            derive_section("https://docs.example.com/en/tutorials/y.html", depth=1, seed_prefixes=seeds),
            "tutorials",
        )

    def test_multiple_seeds_different_hosts(self):
        seeds = [
            ("a.example.com", "/docs/"),
            ("b.example.com", "/help/"),
        ]
        self.assertEqual(
            derive_section("https://a.example.com/docs/reference/x.html", depth=1, seed_prefixes=seeds),
            "reference",
        )
        self.assertEqual(
            derive_section("https://b.example.com/help/faq/y.html", depth=1, seed_prefixes=seeds),
            "faq",
        )

    def test_url_matching_no_seed_falls_back_to_root_relative(self):
        seeds = [("docs.example.com", "/en/stable/")]
        self.assertEqual(
            derive_section("https://other.example.com/a/b/c.html", depth=1, seed_prefixes=seeds),
            "a",
        )


class TestSlugify(unittest.TestCase):
    def test_lowercase_and_hyphenate(self):
        self.assertEqual(slugify("Tutorial/Security"), "tutorial-security")

    def test_collapses_repeated_separators(self):
        self.assertEqual(slugify("a//b__c  d"), "a-b-c-d")

    def test_strips_leading_trailing_hyphens(self):
        self.assertEqual(slugify("/leading-and-trailing/"), "leading-and-trailing")

    def test_empty_label_uses_fallback(self):
        self.assertEqual(slugify(""), FALLBACK_SECTION)


class TestCapSlug(unittest.TestCase):
    def test_short_slug_unchanged(self):
        self.assertEqual(cap_slug("short-slug"), "short-slug")

    def test_long_slug_truncated_with_hash_suffix(self):
        long_slug = "a" * 100
        capped = cap_slug(long_slug, max_length=60)
        self.assertEqual(len(capped), 60)
        self.assertNotEqual(capped, long_slug[:60])  # not a bare truncation

    def test_two_long_slugs_sharing_a_prefix_cap_to_different_strings(self):
        a = "shared-prefix-" + "a" * 100
        b = "shared-prefix-" + "b" * 100
        self.assertNotEqual(cap_slug(a, max_length=60), cap_slug(b, max_length=60))


class TestSanitizeFilenameComponent(unittest.TestCase):
    def test_reserved_windows_name_gets_suffix(self):
        self.assertEqual(sanitize_filename_component("con"), "con-section")
        self.assertEqual(sanitize_filename_component("COM1"), "COM1-section")

    def test_normal_name_unchanged(self):
        self.assertEqual(sanitize_filename_component("tutorial"), "tutorial")


class TestDisambiguateSlugs(unittest.TestCase):
    def test_distinct_labels_get_distinct_slugs(self):
        result = disambiguate_slugs(["tutorial/security", "tutorial/testing"])
        self.assertEqual(len(set(result.values())), 2)

    def test_colliding_labels_get_numeric_suffixes(self):
        # Two different labels that slugify to the same base string.
        result = disambiguate_slugs(["a!b", "a?b"])
        self.assertEqual(len(set(result.values())), 2)
        self.assertIn("a-b", result.values())
        self.assertIn("a-b-1", result.values())

    def test_repeated_label_maps_to_the_same_slug(self):
        result = disambiguate_slugs(["tutorial", "tutorial"])
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
