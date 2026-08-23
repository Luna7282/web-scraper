import unittest

from crawl.sitemap import parse_sitemap_urls

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://x.test/a</loc></url>
  <url><loc>https://x.test/b</loc><lastmod>2026-01-01</lastmod></url>
</urlset>
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://x.test/sitemap-a.xml</loc></sitemap>
  <sitemap><loc>https://x.test/sitemap-b.xml</loc></sitemap>
</sitemapindex>
"""

NO_NAMESPACE = """<urlset><url><loc>https://x.test/a</loc></url></urlset>"""


class TestParseSitemapUrls(unittest.TestCase):
    def test_urlset_extracts_page_urls(self):
        result = parse_sitemap_urls(URLSET)
        self.assertEqual(result.urls, ["https://x.test/a", "https://x.test/b"])
        self.assertEqual(result.sub_sitemaps, [])
        self.assertIsNone(result.parse_error)

    def test_sitemapindex_extracts_sub_sitemaps_not_page_urls(self):
        result = parse_sitemap_urls(SITEMAP_INDEX)
        self.assertEqual(result.urls, [])
        self.assertEqual(result.sub_sitemaps, ["https://x.test/sitemap-a.xml", "https://x.test/sitemap-b.xml"])

    def test_namespace_variance_does_not_break_parsing(self):
        result = parse_sitemap_urls(NO_NAMESPACE)
        self.assertEqual(result.urls, ["https://x.test/a"])

    def test_malformed_xml_reports_error_not_a_crash(self):
        result = parse_sitemap_urls("<urlset><url><loc>unclosed")
        self.assertEqual(result.urls, [])
        self.assertIsNotNone(result.parse_error)


if __name__ == "__main__":
    unittest.main()
