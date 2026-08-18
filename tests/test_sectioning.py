"""Offline tests for sectioning.py -- pure string/URL logic, no I/O."""
import unittest

from sectioning import (
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
