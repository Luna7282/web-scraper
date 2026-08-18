"""robots.txt / sitemap.xml / llms.txt: fetched once per host, cached for
the run only -- not persisted. These don't need to survive a crash (cheap
to refetch, and robots.txt could legitimately change between runs anyway),
so this stays a plain in-memory dict, not part of frontier.py's SQLite
state.

Fetching is injected (fetch_text_fn) so this is fully offline-testable
with a stub -- no network, no dependency on the real HTTP client here.
"""
from __future__ import annotations

import urllib.robotparser
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class HostPolicy:
    host: str
    robots_found: bool = False
    robots_parser: urllib.robotparser.RobotFileParser | None = None
    sitemap_urls: list[str] = field(default_factory=list)
    llms_txt_found: bool = False
    llms_txt: str | None = None

    def is_allowed(self, url: str, user_agent: str = "*") -> bool:
        if self.robots_parser is None:
            return True  # no robots.txt found/parseable -- default allow
        return self.robots_parser.can_fetch(user_agent, url)


FetchTextFn = Callable[[str], Awaitable[str | None]]


class RobotsCache:
    def __init__(self, fetch_text_fn: FetchTextFn, user_agent: str = "*"):
        """fetch_text_fn(url) -> text, or None on any fetch failure (404,
        timeout, connection error, etc) -- treated as 'file does not
        exist', which is the correct default per robots.txt convention
        (absence means everything is allowed)."""
        self._fetch_text = fetch_text_fn
        self._user_agent = user_agent
        self._cache: dict[str, HostPolicy] = {}

    async def get_policy(self, host: str, scheme: str = "https") -> HostPolicy:
        if host in self._cache:
            return self._cache[host]
        base = f"{scheme}://{host}"
        policy = HostPolicy(host=host)

        robots_text = await self._fetch_text(f"{base}/robots.txt")
        if robots_text is not None:
            policy.robots_found = True
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(robots_text.splitlines())
            policy.robots_parser = parser
            for line in robots_text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    policy.sitemap_urls.append(line.split(":", 1)[1].strip())

        # Always also check the conventional location, regardless of
        # whether robots.txt declared one elsewhere -- for reporting
        # completeness, not just crawl-scoping purposes.
        sitemap_text = await self._fetch_text(f"{base}/sitemap.xml")
        if sitemap_text is not None and f"{base}/sitemap.xml" not in policy.sitemap_urls:
            policy.sitemap_urls.append(f"{base}/sitemap.xml")

        llms_text = await self._fetch_text(f"{base}/llms.txt")
        if llms_text is not None:
            policy.llms_txt_found = True
            policy.llms_txt = llms_text

        self._cache[host] = policy
        return policy

    def is_allowed(self, host: str, url: str) -> bool:
        """Caller must have already awaited get_policy(host) at least
        once -- an un-cached host defaults to allowed rather than raising,
        since silently blocking a host nobody checked would be worse than
        the (rare, caller-error) alternative."""
        policy = self._cache.get(host)
        if policy is None:
            return True
        return policy.is_allowed(url, self._user_agent)
