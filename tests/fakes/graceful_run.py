"""A headless run with fake I/O, driven as a subprocess by
tests/test_graceful_stop.py so real signals can be sent to it.

Real: main.headless_v1_entry, headless_crawl, the frontier, workers and
Writer. Fake: the crawler (an async context manager that records when it is
closed -- the stand-in for Chromium being shut down), fetch (a 40-page site
whose pages link forward) and the extractor (sleeps, returns one pair). No
LLM call and no network: the site's host is 127.0.0.1:9, a closed local
port, so even the robots.txt fetch is refused locally.

usage: python -m tests.fakes.graceful_run <run_dir> <mode: v1|legacy> <extract_delay> <grace> [resume]
"""

import asyncio
import json
import os
import sys

import main

HOST = "127.0.0.1:9"
PAGES = 40


def _mark_closed(run_dir):
    with open(os.path.join(run_dir, "crawler_closed"), "w") as f:
        f.write("closed")


class FakeCrawler:
    def __init__(self, run_dir):
        self.run_dir = run_dir

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        _mark_closed(self.run_dir)  # tiny local write; this is a test fake
        return False


def fake_fetch_factory(crawler):
    async def fetch(url):
        await asyncio.sleep(0.02)
        i = int(url.rsplit("/p", 1)[1])
        links = [f"http://{HOST}/p{j}" for j in (i + 1, i + 2) if j < PAGES]
        return f"# Page {i}\n\nSome text about page {i}.", links
    return fetch


def make_extract(delay):
    async def extract(unit):
        await asyncio.sleep(delay)
        return json.dumps([{"instruction": "What is on this page?", "response": unit[:40]}])
    return extract


def main_entry():
    run_dir, mode, delay, grace = sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
    resume = len(sys.argv) > 5 and sys.argv[5] == "resume"
    cfg = {"mode": "crawl", "run_dir": run_dir, "seeds": [[f"http://{HOST}/p0", None]],
           "host_prefix_pairs": [[HOST, "/"]], "provider": "gemini", "model": "fake-model",
           "max_pages": PAGES, "crawl_workers": 2, "extract_workers": 2, "intent": ""}
    seams = {"_crawler_factory": lambda: FakeCrawler(run_dir), "_fetch_fn_factory": fake_fetch_factory,
             "_extract_fn": make_extract(delay)}
    if mode == "legacy":  # no contract_version: no handlers, today's behaviour
        result = asyncio.run(main.headless_crawl(cfg, **seams))
        print(json.dumps(result))
        return 0
    cfg.update({"contract_version": 1, "settings": {"stop_grace_seconds": grace}})
    if resume:
        cfg["resume"] = True

    async def run(c, api_key=None, context=None):
        return await main.headless_crawl(c, api_key=api_key, context=context, **seams)

    return main.headless_v1_entry(cfg, run=run)


if __name__ == "__main__":
    sys.exit(main_entry())
