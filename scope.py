"""URL normalization and crawl-scope decisions.

Pure functions only -- no I/O, no network, fully unit-testable. Kept
deliberately separate: normalize_url() is needed by the frontier's visited
check independently of scoping (step 5), so it must not be folded into
is_in_scope().
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

_ALLOWED_SCHEMES = ("http", "https")


def normalize_url(href: str, base_url: str) -> str | None:
    """Resolve href against base_url and canonicalize it.

    Returns None if href isn't a normalizable http(s) URL: empty/blank,
    mailto:, javascript:, tel:, or anything else that isn't http(s) after
    resolution.

    Canonicalization:
    - relative and protocol-relative hrefs resolved against base_url
    - scheme and host lowercased; path case preserved (servers can be
      case-sensitive on path)
    - fragment always stripped
    - query params sorted, not dropped -- distinct query values can be
      genuinely distinct pages (e.g. ?page=2), so dropping them would
      wrongly conflate different content, not just reorder duplicates
    - trailing slash stripped except for a bare root ("/")
    """
    if href is None:
        return None
    href = href.strip()
    if not href:
        return None

    resolved = urljoin(base_url, href)
    parsed = urlparse(resolved)

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    if not parsed.netloc:
        return None

    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))

    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path,
        parsed.params,
        query,
        "",  # fragment always stripped
    ))


@dataclass(frozen=True)
class DerivedPrefix:
    host: str
    prefix: str | None  # None = degenerate -> host-only, no path restriction
    degenerate: bool
    note: str


def derive_prefix(urls: list[str]) -> DerivedPrefix:
    """Derive a path-prefix scope from a branch's discovered URLs.

    Assumes every url is already normalized (normalize_url()) and shares
    one host -- true by construction, since discover_branches buckets by
    host before this is ever called.

    For branches with more than one distinct path, takes the longest
    common path across all of them *as-is*, with no per-URL adjustment.
    This is deliberate: a section's own index page (e.g. "/tutorial",
    alongside "/tutorial/security", "/tutorial/body", ...) is already a
    valid prefix of its siblings, so commonpath() finds the right boundary
    on its own. An earlier version of this function took the *dirname* of
    every URL before computing the common path, on the theory that this
    would handle a single-URL branch's leaf-file case -- but applied
    universally, that destroyed exactly this common index-page pattern,
    dragging branches like fastapi.tiangolo.com's 51-URL "/tutorial/*"
    down to a fully degenerate host-only scope, discovered by testing
    against a second real site rather than only the one this predicate
    was first written against. See LESSONS_LEARNED.md #8.

    For a branch with exactly one distinct path (no sibling data to
    disambiguate "this URL is a directory" from "this URL is a leaf
    file"), the *dirname* heuristic is still applied, so a single leaf
    page still yields a directory-level prefix instead of one exact URL
    nothing else can ever match. This remains a real, explicit heuristic
    limitation for the single-URL case specifically: if that one URL is
    itself a section index (e.g. a branch containing only "/tutorial"),
    this derives the prefix one level *above* it. There's no way to tell
    a directory-style URL from a leaf page without fetching it, which this
    pure function deliberately doesn't do.

    Degenerate case: if the common path collapses to the site root ("/"),
    the branch's URLs share no meaningful sub-path. Explicit decision, not
    left emergent: fall back to host-only scope (prefix=None) with a
    warning, rather than rejecting the branch or requiring manual entry.
    Host-only is still safely bounded (never crosses to another domain)
    and is exactly correct for a genuinely root-level category (e.g.
    discovery's "Root Level" bucket) -- it only over-broadens for a
    category that degenerates by coincidence.
    """
    if not urls:
        raise ValueError("derive_prefix requires at least one URL")

    parsed = [urlparse(u) for u in urls]
    host = parsed[0].netloc.lower()
    paths = [p.path or "/" for p in parsed]

    if len(set(paths)) == 1:
        common = posixpath.dirname(paths[0]) or "/"
    else:
        common = posixpath.commonpath(paths)

    if not common.endswith("/"):
        common += "/"

    if common == "/":
        return DerivedPrefix(
            host=host,
            prefix=None,
            degenerate=True,
            note=(
                f"URLs share no path beyond the site root on {host} -- "
                f"falling back to host-only scope (any path on this host "
                f"is in-scope). Correct if this is genuinely a root-level "
                f"category; broadens scope if it isn't."
            ),
        )
    return DerivedPrefix(
        host=host, prefix=common, degenerate=False, note=f"derived prefix: {common}"
    )


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    reason: str


def is_in_scope(
    normalized_url: str,
    host: str,
    prefix: str | None,
    visited: set[str] | None = None,
) -> ScopeDecision:
    """Decide whether an already-normalized URL is in scope.

    Does NOT normalize -- callers must run normalize_url() first. Keeps
    normalization and scope policy independently testable and reusable;
    the frontier needs normalization for its visited check regardless of
    whether scoping is even being applied.
    """
    host = host.lower()

    if visited and normalized_url in visited:
        return ScopeDecision(False, "already_visited")

    parsed = urlparse(normalized_url)
    if parsed.netloc != host:
        return ScopeDecision(False, f"off_host ({parsed.netloc or 'no host'})")

    if prefix is not None and not (parsed.path + "/").startswith(prefix):
        # prefix always ends in "/" (a directory boundary). Comparing
        # path+"/" against it, rather than path itself, means a section's
        # own bare index URL (e.g. "/how-to", no trailing slash after
        # normalize_url strips it) still matches its own prefix
        # ("/how-to/") -- and a sibling section that merely shares a text
        # prefix (e.g. "/how-toz") still correctly does not.
        return ScopeDecision(False, f"outside_prefix (not under {prefix})")

    return ScopeDecision(True, "accepted")
