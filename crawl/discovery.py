from urllib.parse import urlparse
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

from crawl.scope import normalize_url

async def fetch_page_links(url: str) -> list[str]:
    """Fetch one page and return its outbound hrefs exactly as found --
    unresolved, unfiltered, including any non-http hrefs (mailto:,
    javascript:, #-only anchors, etc). Normalization/filtering is the
    caller's job (see discover_branches below, or the --dry-run path in
    main.py, which both call normalize_url() on this same raw list).

    Cached (CacheMode.ENABLED): crawl4ai persists the fetched page locally,
    so re-running discovery/dry-run against the same URL during iteration
    doesn't re-hit the site.
    """
    async with AsyncWebCrawler() as crawler:
        config = CrawlerRunConfig(cache_mode=CacheMode.ENABLED)
        result = await crawler.arun(url=url, config=config)
        links = result.links.get("internal", []) + result.links.get("external", [])
        return [link_obj.get("href", "") for link_obj in links if link_obj.get("href")]

async def discover_branches(root_url: str):
    print(f"Discovering branches from {root_url}...")
    try:
        hrefs = await fetch_page_links(root_url)
        root_host = urlparse(root_url).netloc.lower()

        branches = {}
        for href in hrefs:
            url = normalize_url(href, base_url=root_url)
            if url is None:
                continue

            parsed = urlparse(url)
            domain = parsed.netloc
            path_parts = [p for p in parsed.path.split('/') if p]

            if domain != root_host:
                category = f"External: {domain}"
            else:
                if path_parts:
                    category = f"Path: /{path_parts[0]}/*"
                else:
                    category = "Root Level"

            if category not in branches:
                branches[category] = set()
            branches[category].add(url)

        return {k: list(v) for k, v in branches.items()}

    except Exception as e:
        print(f"Discovery failed: {e}")
        return {}
