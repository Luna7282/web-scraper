"""Section derivation and filename-safe slugification -- two distinct
transforms, deliberately not collapsed (step 8 plan, Part C).

derive_section(url, depth) produces a human-readable path-prefix label
used as the canonical record's "section" field. slugify/cap_slug/
disambiguate_slugs turn a set of those labels into names safe to use as
files on disk -- this repo runs on Windows (D:\\scraper), so reserved
device names and path-length limits are a live constraint, not a
hypothetical one.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

DEFAULT_SECTION_DEPTH = 2
MAX_SLUG_LENGTH = 60
FALLBACK_SECTION = "root"

_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def derive_section(
    url: str,
    depth: int = DEFAULT_SECTION_DEPTH,
    seed_prefixes: list[tuple[str, str | None]] | None = None,
) -> str:
    """First `depth` non-empty path segments, counted from the crawl's own
    seed prefix (not the domain root), joined with '/'. Depth-from-root
    was confirmed broken on real data (step 8 Phase 2A): a site crawled
    from /en/stable/ produced "en/stable" as literally every page's
    section, since the locale+version segments the seed itself sits
    behind ate the whole depth budget before any real category (reference,
    tutorials, ...) was ever reached.

    seed_prefixes is the same (host, prefix) list main.py already builds
    for scope_check (crawl/scope.py::derive_prefix per selected branch) --
    reused, not reimplemented. A crawl with multiple seeds/branches on
    different prefixes is resolved by longest-matching-prefix, the same
    convention crawl/scope.py's own prefix matching uses: a URL can
    legitimately fall under more than one selected branch's prefix if
    they nest, and the most specific one should win.

    seed_prefixes=None (the default) falls back to the original
    root-relative behavior -- existing callers that never had seed
    context (tests, or a URL that doesn't match any known seed) still get
    a sensible answer instead of an error.

    A site's root/index page (empty relative path) maps to
    FALLBACK_SECTION rather than an empty string -- an empty label would
    otherwise slugify to an empty filename, which is exactly the
    degenerate case this exists to avoid.
    """
    parsed = urlparse(url)
    relative_path = parsed.path

    if seed_prefixes:
        matches = [
            prefix for host, prefix in seed_prefixes
            if host == parsed.netloc and prefix and (parsed.path + "/").startswith(prefix)
        ]
        if matches:
            best_prefix = max(matches, key=len)
            relative_path = parsed.path[len(best_prefix):]

    segments = [s for s in relative_path.split("/") if s]
    if not segments:
        return FALLBACK_SECTION
    return "/".join(segments[:depth])


def slugify(label: str) -> str:
    slug = _SLUG_UNSAFE_RE.sub("-", label.lower()).strip("-")
    return slug or FALLBACK_SECTION


def cap_slug(slug: str, max_length: int = MAX_SLUG_LENGTH) -> str:
    """Truncates long slugs, appending a short content hash rather than
    just cutting the string -- two long slugs sharing the same first
    (max_length - 9) chars would otherwise silently truncate to the exact
    same string before disambiguate_slugs ever gets a chance to notice
    they were different."""
    if len(slug) <= max_length:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    return f"{slug[: max_length - 9]}-{digest}"


def sanitize_filename_component(slug: str) -> str:
    """CON, PRN, AUX, NUL, COM1-9, LPT1-9 are illegal Windows filenames
    regardless of case or extension -- a section literally named "aux"
    (plausible: an i18n "aux" locale code, an "aux" API section) would
    otherwise fail to open as a file with no obvious reason why."""
    if slug.upper() in _RESERVED_WINDOWS_NAMES:
        return f"{slug}-section"
    return slug


def disambiguate_slugs(labels: list[str]) -> dict[str, str]:
    """Maps each distinct section label to a unique, filesystem-safe slug.
    Two different labels that collide after slugification/truncation get
    a numeric suffix instead of silently merging into one file -- silent
    merging would combine two sections' rows with no record it happened."""
    result: dict[str, str] = {}
    used: dict[str, int] = {}
    for label in labels:
        if label in result:
            continue
        base = sanitize_filename_component(cap_slug(slugify(label)))
        if base not in used:
            used[base] = 0
            result[label] = base
        else:
            used[base] += 1
            result[label] = f"{base}-{used[base]}"
    return result
