"""One-off tool: fetch each test site once, cache it as tests/fixtures/
file triples (server-rendered HTML, browser-rendered HTML, JSON metadata),
so scope-predicate tests -- and step 4's chrome-stripping-selector work --
run offline against real, reproducible data instead of hitting the network
or depending on crawl4ai's own opaque cache directory.

Not part of the pipeline -- run manually to (re)generate fixtures:
    python tests/fetch_fixtures.py

For each site, makes two requests:
  1. plain requests.get() (no browser, no JS; normal browser User-Agent,
     redirects followed) -- the server-rendered baseline.
  2. crawl4ai (real headless browser, JS executed) -- the pipeline's own
     fetch path, browser-rendered.

Both HTML bodies are cached. hrefs from both are extracted, normalized
(via scope.normalize_url, so the comparison isn't confounded by relative-
vs-absolute formatting differences between the two extraction methods),
and diffed into static-only / rendered-only / shared sets -- a same-count
comparison can hide client-side routing swapping one link set for another,
so counts alone aren't a safe proxy.
"""
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scope import normalize_url

FIXTURES_DIR = Path(__file__).parent / "fixtures"

SITES = {
    "docs_manim_community": "https://docs.manim.community/en/stable/",
    "fastapi_tiangolo": "https://fastapi.tiangolo.com/",
    "www_manim_community": "https://www.manim.community/",
    "stackblitz": "https://stackblitz.com/",
    "blog_cloudflare": "https://blog.cloudflare.com/",
}

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_NORMAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def fetch_raw(url: str):
    """Plain HTTP GET, no browser, no JS -- the server-rendered baseline."""
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": _NORMAL_UA}, allow_redirects=True)
        hrefs = _HREF_RE.findall(resp.text)
        return {
            "html": resp.text,
            "status_code": resp.status_code,
            "final_url": resp.url,
            "hrefs": hrefs,
        }
    except Exception as e:
        return {"html": "", "status_code": None, "final_url": None, "hrefs": [], "error": str(e)}


async def fetch_rendered(url: str):
    """crawl4ai's real fetch path -- headless browser, JS executed."""
    async with AsyncWebCrawler() as crawler:
        config = CrawlerRunConfig(cache_mode=CacheMode.ENABLED)
        result = await crawler.arun(url=url, config=config)
        links = result.links.get("internal", []) + result.links.get("external", [])
        hrefs = [l.get("href", "") for l in links if l.get("href")]
        return {
            "html": result.html,
            "status_code": result.redirected_status_code or result.status_code,
            "final_url": result.redirected_url or result.url,
            "hrefs": hrefs,
        }


def _normalized_set(hrefs: list[str], base_url: str) -> set[str]:
    out = set()
    for h in hrefs:
        n = normalize_url(h, base_url)
        if n is not None:
            out.add(n)
    return out


async def fetch_one(slug: str, url: str):
    print(f"Fetching {slug}: {url}")
    raw = fetch_raw(url)
    rendered = await fetch_rendered(url)

    base_for_normalization = rendered["final_url"] or raw["final_url"] or url
    raw_set = _normalized_set(raw["hrefs"], base_for_normalization)
    rendered_set = _normalized_set(rendered["hrefs"], base_for_normalization)

    static_only = sorted(raw_set - rendered_set)
    rendered_only = sorted(rendered_set - raw_set)
    shared = sorted(raw_set & rendered_set)
    union_size = len(raw_set | rendered_set)
    overlap_ratio = (len(shared) / union_size) if union_size else 1.0

    if union_size == 0:
        required_rendering, basis = False, "no hrefs found in either version"
    elif len(raw_set) == 0:
        required_rendering, basis = True, "0 hrefs in server-rendered HTML, links only appear after browser rendering"
    elif overlap_ratio < 0.5:
        required_rendering, basis = True, f"only {overlap_ratio:.0%} of the combined unique links are shared between server- and browser-rendered versions"
    else:
        required_rendering, basis = False, f"{overlap_ratio:.0%} of unique links shared between versions"

    (FIXTURES_DIR / f"{slug}.server.html").write_text(raw["html"], encoding="utf-8")
    (FIXTURES_DIR / f"{slug}.rendered.html").write_text(rendered["html"], encoding="utf-8")

    metadata = {
        "slug": slug,
        "root_url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "server_rendered": {
            "final_url": raw["final_url"],
            "status_code": raw["status_code"],
            "href_count": len(raw["hrefs"]),
        },
        "browser_rendered": {
            "final_url": rendered["final_url"],
            "status_code": rendered["status_code"],
            "href_count": len(rendered["hrefs"]),
            "hrefs": rendered["hrefs"],
        },
        "href_set_comparison": {
            "static_only": static_only,
            "rendered_only": rendered_only,
            "shared": shared,
            "overlap_ratio": round(overlap_ratio, 3),
        },
        "required_rendering": required_rendering,
        "required_rendering_basis": basis,
    }
    (FIXTURES_DIR / f"{slug}.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(
        f"  -> server={len(raw_set)} rendered={len(rendered_set)} shared={len(shared)} "
        f"required_rendering={required_rendering} ({basis})"
    )


async def main():
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for slug, url in SITES.items():
        try:
            await fetch_one(slug, url)
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
