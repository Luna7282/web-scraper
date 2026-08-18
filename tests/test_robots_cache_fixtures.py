"""RobotsCache tested against real, cached robots.txt/sitemap.xml/llms.txt
responses from the 5 fixture sites (tests/fixtures/robots/), including the
genuine misses (a 404 is a real case the parser has to handle correctly,
not just something to simulate). Offline -- reads cached files only.
"""
import json
import unittest
from pathlib import Path

from robots_cache import RobotsCache

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "robots"

SLUGS = [
    "docs_manim_community",
    "fastapi_tiangolo",
    "www_manim_community",
    "stackblitz",
    "blog_cloudflare",
]


def make_fixture_fetcher(slug: str, report: dict):
    """Serves the cached fixture content for exactly the URLs RobotsCache
    would request, replaying the real status codes (None on anything that
    wasn't a 200, matching what the real fetch layer should also do)."""
    robots_path = FIXTURES_DIR / f"{slug}_robots.txt"
    sitemap_path = FIXTURES_DIR / f"{slug}_sitemap.xml"

    async def fetch_text(url: str) -> str | None:
        if url.endswith("/robots.txt"):
            return robots_path.read_text(encoding="utf-8") if report["robots_found"] else None
        if url.endswith("/sitemap.xml"):
            return sitemap_path.read_text(encoding="utf-8") if report["sitemap_found"] else None
        if url.endswith("/llms.txt"):
            return None  # none of the 5 sites have one -- verified live
        raise AssertionError(f"unexpected URL requested: {url}")

    return fetch_text


class TestRobotsCacheAgainstRealFixtures(unittest.IsolatedAsyncioTestCase):
    async def test_docs_manim_community_disallows_root_for_everyone(self):
        report = json.loads((FIXTURES_DIR / "docs_manim_community.json").read_text())
        cache = RobotsCache(make_fixture_fetcher("docs_manim_community", report))
        policy = await cache.get_policy("docs.manim.community")
        self.assertTrue(policy.robots_found)
        # Real robots.txt: "Disallow: /" for User-agent: * -- everything
        # blocked except whatever's explicitly allowed underneath it.
        self.assertFalse(policy.is_allowed("https://docs.manim.community/en/stable/"))

    async def test_stackblitz_missing_robots_txt_defaults_allowed(self):
        # Genuine 404, not a synthetic one -- confirms the real miss path.
        report = json.loads((FIXTURES_DIR / "stackblitz.json").read_text())
        self.assertEqual(report["robots_status_code"], 404)
        cache = RobotsCache(make_fixture_fetcher("stackblitz", report))
        policy = await cache.get_policy("stackblitz.com")
        self.assertFalse(policy.robots_found)
        self.assertTrue(policy.is_allowed("https://stackblitz.com/anything"))

    async def test_blog_cloudflare_sitemap_is_a_sitemapindex_not_urlset(self):
        # Real-world nested-sitemap case -- RobotsCache only records the
        # URL, doesn't fetch/parse it; recursive handling is future work
        # (ROADMAP), this just confirms the shape is detectable from what's
        # already cached.
        report = json.loads((FIXTURES_DIR / "blog_cloudflare.json").read_text())
        self.assertEqual(report["sitemap_root_tag"], "sitemapindex")
        cache = RobotsCache(make_fixture_fetcher("blog_cloudflare", report))
        policy = await cache.get_policy("blog.cloudflare.com")
        self.assertIn("https://blog.cloudflare.com/sitemap.xml", policy.sitemap_urls)

    async def test_fastapi_allows_everything(self):
        report = json.loads((FIXTURES_DIR / "fastapi_tiangolo.json").read_text())
        cache = RobotsCache(make_fixture_fetcher("fastapi_tiangolo", report))
        policy = await cache.get_policy("fastapi.tiangolo.com")
        self.assertTrue(policy.is_allowed("https://fastapi.tiangolo.com/tutorial/"))

    async def test_none_of_the_five_sites_have_llms_txt(self):
        for slug in SLUGS:
            report = json.loads((FIXTURES_DIR / f"{slug}.json").read_text())
            self.assertFalse(report["llms_found"], f"{slug} unexpectedly has llms.txt")

    async def test_crawl_delay_present_in_none_of_the_five_but_would_be_silently_ignored(self):
        # None of the 5 sites specify Crawl-delay, so this can't be proven
        # against real data -- but confirmed directly against
        # RobotFileParser (see LESSONS_LEARNED.md): it parses Crawl-delay
        # fine, HostPolicy just never reads it. Documented as a real gap,
        # not fabricated evidence against sites that don't exercise it.
        for slug in SLUGS:
            report = json.loads((FIXTURES_DIR / f"{slug}.json").read_text())
            self.assertIsNone(report.get("crawl_delay"))


if __name__ == "__main__":
    unittest.main()
