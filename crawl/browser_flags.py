"""Never hide automation from sites (ROADMAP #70, HIGH).

crawl4ai (0.9.2) always adds `--disable-blink-features=AutomationControlled`
to Chromium's launch args, in both builders: `BrowserManager._build_browser_args`
(the Playwright launch path this scraper uses through `AsyncWebCrawler()`) and
`ManagedBrowser.build_browser_flags` (the managed / CDP path). That flag hides
`navigator.webdriver`: an anti-detection feature, against the never-stealth
rule. crawl4ai has no setting to drop a default arg, so `install()` wraps both
builders and removes every arg in `config.BROWSER_FORBIDDEN_ARGS`. main.py
calls it at import, before any crawler starts. Idempotent.
"""

from __future__ import annotations

import config

_MARK = "_scraper_strips_forbidden_args"


def strip(args: list[str]) -> list[str]:
    return [a for a in args if a not in config.BROWSER_FORBIDDEN_ARGS]


def install() -> None:
    from crawl4ai import browser_manager as bm

    build = bm.BrowserManager._build_browser_args
    if not getattr(build, _MARK, False):
        def _build_browser_args(self):
            out = build(self)
            out["args"] = strip(list(out.get("args", [])))
            return out
        setattr(_build_browser_args, _MARK, True)
        _build_browser_args.original = build
        bm.BrowserManager._build_browser_args = _build_browser_args

    flags = bm.ManagedBrowser.build_browser_flags
    if not getattr(flags, _MARK, False):
        def build_browser_flags(browser_config):
            return strip(list(flags(browser_config)))
        setattr(build_browser_flags, _MARK, True)
        build_browser_flags.original = flags
        bm.ManagedBrowser.build_browser_flags = staticmethod(build_browser_flags)
