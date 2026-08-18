"""robots.txt / sitemap.xml / llms.txt: fetched once per host, cached for
the run only -- not persisted. These don't need to survive a crash (cheap
to refetch, and robots.txt could legitimately change between runs anyway),
so this stays a plain in-memory dict, not part of frontier.py's SQLite
state.

Fetching is injected (fetch_text_fn) so this is fully offline-testable
with a stub -- no network, no dependency on the real HTTP client here.

Allow/Disallow resolution is a from-scratch implementation, not
stdlib urllib.robotparser -- confirmed by direct reproduction that
robotparser resolves rules in FILE ORDER (first matching rule wins),
not by specificity. Per RFC 9309 (and every major real crawler), the
longest matching pattern wins regardless of order, with Allow breaking
a tie against Disallow. This is not a hypothetical distinction:
docs.manim.community's real robots.txt is
    User-agent: *
    Disallow: /
    Allow: /en/stable/
-- robotparser.can_fetch() returns False for /en/stable/ itself (the
one path this site's owner explicitly wants crawled), because the
blanket "Disallow: /" is checked first and matches every path. A step-5
test asserted that exact wrong answer as correct (see git history on
tests/test_robots_cache_fixtures.py) -- caught only once a live crawl
against this exact site actually exercised the path, not by the
fixture-based test suite, which recorded the robots.txt's shape but
never asserted the one URL that mattered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlparse


@dataclass(frozen=True)
class _Rule:
    directive: str  # "allow" or "disallow"
    pattern: str


def _pattern_to_regex(pattern: str) -> re.Pattern:
    """robots.txt patterns: '*' matches any sequence, a trailing '$'
    anchors end-of-path -- both widely-supported extensions beyond the
    original spec, not exotic. Everything else is a literal prefix."""
    end_anchor = pattern.endswith("$")
    body = pattern[:-1] if end_anchor else pattern
    escaped = re.escape(body).replace(r"\*", ".*")
    if end_anchor:
        escaped += "$"
    return re.compile("^" + escaped)


def _parse_rules(text: str, user_agent: str) -> list[_Rule]:
    """RFC 9309 group selection: an exact user-agent match wins over the
    '*' group; consecutive User-agent lines with no directive between
    them belong to the same group (a robots.txt convention -- one rule
    set applying to multiple named agents)."""
    groups: dict[str, list[_Rule]] = {}
    current_agents: list[str] = []
    group_open = False  # true while still accepting more User-agent lines into current_agents

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()

        if key == "user-agent":
            if not group_open:
                current_agents = []
            current_agents.append(value.lower())
            groups.setdefault(value.lower(), [])
            group_open = True
        elif key in ("allow", "disallow") and current_agents:
            group_open = False
            if value == "" and key == "disallow":
                continue  # an empty Disallow value means "allow everything" by convention
            for agent in current_agents:
                groups[agent].append(_Rule(directive=key, pattern=value))

    ua = user_agent.lower()
    if ua in groups:
        return groups[ua]
    if "*" in groups:
        return groups["*"]
    return []


def _is_path_allowed(rules: list[_Rule], path: str) -> bool:
    """Longest matching pattern wins; a tie between an Allow and a
    Disallow of equal length resolves to Allow (the less restrictive
    rule), per RFC 9309. No matching rule at all means allowed."""
    best_length, best_directive = -1, "allow"
    for rule in rules:
        if _pattern_to_regex(rule.pattern).match(path):
            length = len(rule.pattern)
            if length > best_length or (length == best_length and rule.directive == "allow"):
                best_length, best_directive = length, rule.directive
    return best_directive == "allow"


@dataclass
class HostPolicy:
    host: str
    robots_found: bool = False
    _rules: list[_Rule] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)
    llms_txt_found: bool = False
    llms_txt: str | None = None

    def is_allowed(self, url: str) -> bool:
        """No per-call user_agent parameter -- unlike stdlib robotparser's
        can_fetch(), the applicable rule group is already resolved once,
        at get_policy() time, against RobotsCache's configured
        user_agent. A parameter that looked like it selected a group per
        call but silently didn't would be a worse API than not having
        it."""
        if not self.robots_found:
            return True  # no robots.txt found/parseable -- default allow
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return _is_path_allowed(self._rules, path)


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
            policy._rules = _parse_rules(robots_text, self._user_agent)
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
        return policy.is_allowed(url)
