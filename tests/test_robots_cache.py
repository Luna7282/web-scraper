"""Offline tests for robots_cache.py -- stub fetch_text_fn, no network."""
import unittest

from robots_cache import RobotsCache


def make_stub(responses: dict[str, str | None]):
    async def fetch_text(url: str) -> str | None:
        return responses.get(url)
    return fetch_text


class TestRobotsCache(unittest.IsolatedAsyncioTestCase):
    async def test_disallowed_path_respected(self):
        cache = RobotsCache(make_stub({
            "https://x.com/robots.txt": "User-agent: *\nDisallow: /private/\n",
        }))
        policy = await cache.get_policy("x.com")
        self.assertFalse(policy.is_allowed("https://x.com/private/secret"))
        self.assertTrue(policy.is_allowed("https://x.com/public/page"))
        self.assertFalse(cache.is_allowed("x.com", "https://x.com/private/secret"))

    async def test_missing_robots_txt_defaults_allowed(self):
        cache = RobotsCache(make_stub({}))  # every fetch returns None (404-like)
        policy = await cache.get_policy("x.com")
        self.assertFalse(policy.robots_found)
        self.assertTrue(policy.is_allowed("https://x.com/anything"))

    async def test_sitemap_declared_in_robots_txt_captured(self):
        cache = RobotsCache(make_stub({
            "https://x.com/robots.txt": "User-agent: *\nSitemap: https://x.com/custom-sitemap.xml\n",
        }))
        policy = await cache.get_policy("x.com")
        self.assertIn("https://x.com/custom-sitemap.xml", policy.sitemap_urls)

    async def test_conventional_sitemap_checked_even_when_robots_declares_another(self):
        cache = RobotsCache(make_stub({
            "https://x.com/robots.txt": "Sitemap: https://x.com/custom.xml\n",
            "https://x.com/sitemap.xml": "<urlset></urlset>",
        }))
        policy = await cache.get_policy("x.com")
        self.assertIn("https://x.com/custom.xml", policy.sitemap_urls)
        self.assertIn("https://x.com/sitemap.xml", policy.sitemap_urls)

    async def test_llms_txt_captured_when_present(self):
        cache = RobotsCache(make_stub({
            "https://x.com/llms.txt": "# My site\n\nSome content.",
        }))
        policy = await cache.get_policy("x.com")
        self.assertTrue(policy.llms_txt_found)
        self.assertEqual(policy.llms_txt, "# My site\n\nSome content.")

    async def test_result_cached_second_call_does_not_refetch(self):
        calls = []

        async def fetch_text(url):
            calls.append(url)
            return None

        cache = RobotsCache(fetch_text)
        await cache.get_policy("x.com")
        await cache.get_policy("x.com")
        # 3 fetches (robots.txt, sitemap.xml, llms.txt) on the FIRST call only
        self.assertEqual(len(calls), 3)

    async def test_uncached_host_defaults_allowed(self):
        cache = RobotsCache(make_stub({}))
        self.assertTrue(cache.is_allowed("never-checked.com", "https://never-checked.com/x"))


if __name__ == "__main__":
    unittest.main()
