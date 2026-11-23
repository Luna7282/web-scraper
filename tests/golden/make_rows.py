"""Produces canonical.jsonl bytes from a deterministic v1 or legacy headless
run (fake crawler, fetch and extractor; frozen clock; fixed version and run
id). tests/test_v1_golden.py compares the current code's bytes with golden
files produced by this same script from web-scraper main 1112ad7 (before
Brief 3B). usage (cwd = the code under test): python <this file> <v1|legacy> <out_file>
"""
import asyncio
import io
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime

sys.path.insert(0, os.getcwd())
import main
from crawl import pipeline

FROZEN = datetime(2026, 11, 20, 18, 0, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz is not None else FROZEN.replace(tzinfo=None)


pipeline.datetime = _FrozenDatetime
HOST = "127.0.0.1:9"


class _Crawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _site(crawler):
    async def fetch(url):
        i = int(url.rstrip("/").rsplit("/p", 1)[1])
        text = f"# Page {i}\n\nPage {i} explains topic {i} in plain words. It has two sentences."
        return text, [f"http://{HOST}/p{i + 1}"] if i < 2 else []
    return fetch


async def _extract(unit):
    n = unit.split("Page ", 1)[1].split()[0]
    return ('[{"instruction": "What does page ' + n + ' explain?", "response": "Topic ' + n
            + ', in plain words."}, {"instruction": "How many sentences?", "response": "Two, ünïcode kept."}]')


def run(mode: str, out_file: str) -> None:
    run_dir = tempfile.mkdtemp(prefix="golden-")
    cfg = {"mode": "crawl", "run_dir": run_dir, "seeds": [[f"http://{HOST}/p0", None]],
           "host_prefix_pairs": [[HOST, "/"]], "provider": "ollama_cloud", "model": "golden-model",
           "max_pages": 3, "crawl_workers": 1, "extract_workers": 1, "intent": ""}
    seams = {"_crawler_factory": _Crawler, "_fetch_fn_factory": _site, "_extract_fn": _extract}
    try:
        if mode == "legacy":
            asyncio.run(main.headless_crawl(cfg, **seams))
        else:
            cfg.update({"contract_version": 1, "run_id": "golden-run", "settings": {"llm_breaker_threshold": 0}})

            async def runner(c, api_key=None, context=None):
                return await main.headless_crawl(c, api_key=api_key, context=context, **seams)
            main.headless_v1_entry(cfg, run=runner, out=io.StringIO(), err=io.StringIO(),
                                   environ={"OLLAMA_API_KEY": "dummy"},
                                   version={"version": "golden", "commit": "golden", "dirty": False})
        shutil.copyfile(os.path.join(run_dir, "canonical.jsonl"), out_file)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
