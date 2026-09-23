import argparse
import asyncio
import json
import os
import sys
import time

import requests
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
from langchain_core.messages import SystemMessage, HumanMessage
from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.table import Table

import config
from llm_factory import get_llm
from crawl.discovery import discover_branches, fetch_page_links
from crawl.scope import normalize_url, derive_prefix, is_in_scope
from crawl.frontier import Frontier
from crawl.pipeline import crawl_worker, extract_worker, writer_worker, map_worker, FetchTimeout, FetchHTTPError, RateLimitError, BrowserDriverError
from crawl.politeness import HostPoliteness
from content.extraction import QA_EXTRACTION_SYSTEM_PROMPT, CODE_QA_EXTRACTION_SYSTEM_PROMPT
from crawl.branch_select import suggest_branch_selection, BRANCH_CLASSIFICATION_SYSTEM_PROMPT
from content.relevance import make_score_fn
from content.sample_quality import make_quality_backend
from crawl.robots_cache import RobotsCache
from crawl.graph import discovery_graph, path_tree, to_json
from crawl.live_status import read_live_status
from storage.writer import Writer
from progress_display import run_progress_display
from crawl.score_report import format_score_report
from storage.chunk_store import ChunkStore, get_or_create_collection, split_into_parent_child_chunks
from storage.query import query_chunks, format_query_results
from content.extraction_units import make_extraction_units_fn
from export.dataset_report import build_dataset_report
from export.export import load_canonical_records
from content.chrome_strip import DEFAULT_EXCLUDED_SELECTOR, DEFAULT_EXCLUDED_TAGS, normalize_link_text, strip_text_patterns

EMBEDDING_MODEL_NAME = "nomic-embed-text"  # LocalOllamaEmbeddings' default; keep in sync (see get_llm)
EMBEDDING_DIM = 768  # measured in step 6 -- see LESSONS_LEARNED.md #17

# orchestrator.py / output_manager.py: the old pipeline. Deliberately left
# importable but unreferenced here -- this file is the rewrite (see the
# rebuild plan and CLAUDE.md); deleting the old modules is step 9's job,
# once the new path has actually completed a real crawl.

console = Console()

DATA_DIR = "data"
RUN_DIR = os.path.join(DATA_DIR, "run")  # run state -- frontier.db, canonical.jsonl, the Chroma index; never a deliverable, see CLAUDE.md's output layout
FRONTIER_DB_PATH = os.path.join(RUN_DIR, "frontier.db")
CANONICAL_PATH = os.path.join(RUN_DIR, "canonical.jsonl")  # one row per Q&A pair, never a training file directly -- see export.py
HTTP_USER_AGENT = "Mozilla/5.0 (compatible; scraper/1.0)"


async def dry_run(root_url: str):
    """Evaluate every href a real discovery call actually returns against
    each branch's derived scope, printing accept/reject + reason, grouped
    by decision. No crawling, no LLM calls -- one network fetch of the
    root page (cached by crawl4ai, so repeat runs during iteration don't
    re-hit the site)."""
    console.print(Panel.fit(f"[bold blue]Dry run: {root_url}[/bold blue]"))

    hrefs = await fetch_page_links(root_url)
    all_urls = set()
    invalid_hrefs = []
    for href in hrefs:
        url = normalize_url(href, base_url=root_url)
        if url is None:
            invalid_hrefs.append(href)
        else:
            all_urls.add(url)

    console.print(
        f"Fetched {len(hrefs)} raw hrefs -> {len(all_urls)} normalized unique "
        f"http(s) URLs, {len(invalid_hrefs)} rejected before scoping (not a "
        f"normalizable http(s) URL: mailto:, javascript:, etc).\n"
    )
    if invalid_hrefs:
        console.print(f"[red]REJECTED at normalization ({len(invalid_hrefs)})[/red]")
        for h in sorted(set(invalid_hrefs)):
            console.print(f"    - {h!r}")
        console.print()

    branches = await discover_branches(root_url)
    if not branches:
        console.print("[red]No branches discovered.[/red]")
        return

    for category, urls in branches.items():
        prefix_result = derive_prefix(urls)
        console.print(f"[bold]{category}[/bold] ({len(urls)} URLs in this branch)")
        console.print(f"  host={prefix_result.host}  prefix={prefix_result.prefix!r}")
        if prefix_result.degenerate:
            console.print(f"  [yellow]{prefix_result.note}[/yellow]")
        else:
            console.print(f"  {prefix_result.note}")

        accepted = []
        rejected_by_kind = {}
        for url in sorted(all_urls):
            decision = is_in_scope(url, host=prefix_result.host, prefix=prefix_result.prefix)
            if decision.allowed:
                accepted.append(url)
            else:
                kind = decision.reason.split(" ", 1)[0]
                rejected_by_kind.setdefault(kind, []).append((url, decision.reason))

        console.print(f"  [green]ACCEPTED ({len(accepted)})[/green]")
        for u in accepted:
            console.print(f"    + {u}")

        for kind in sorted(rejected_by_kind):
            group = rejected_by_kind[kind]
            console.print(f"  [red]REJECTED: {kind} ({len(group)})[/red]")
            for u, reason in group:
                console.print(f"    - {u}  ({reason})")
        console.print()


async def discover_command(root_url: str) -> dict:
    """The non-interactive core of main()'s branch-discovery step, as
    plain data instead of a Rich table -- the server's /runs/discover
    endpoint calls this directly (one bounded page-fetch, a few seconds,
    run via a subprocess -- see server/jobs.py -- never inside the
    server's own process, same "no crawl4ai browser in the long-lived
    server loop" discipline as the crawl/map pipeline itself). Also
    reachable from the CLI via --discover-json for curl-testability
    without the server running at all.

    Each branch's full url list is included (not just a count) --
    starting a run needs the actual seed urls, and discovery is the only
    place that ever fetches the root page to get them; a headless
    start-run call takes them as already-resolved input (see
    headless_map/headless_crawl) rather than re-discovering on every
    start, resume, or retry.
    """
    branches = await discover_branches(root_url)
    result = {"root_url": root_url, "branches": []}
    for category, urls in branches.items():
        prefix_result = derive_prefix(urls)
        result["branches"].append({
            "category": category,
            "urls": sorted(urls),
            "url_count": len(urls),
            "host": prefix_result.host,
            "prefix": prefix_result.prefix,
            "degenerate": prefix_result.degenerate,
            "note": prefix_result.note,
        })
    return result


_DEFAULT_MODEL_BY_PROVIDER = {
    # nvidia: "meta/llama3-70b-instruct" 404'd -- removed from NVIDIA's
    # catalog (confirmed via GET /v1/models, 2026-08-25). Replaced with
    # meta/llama-3.3-70b-instruct, verified against 4 real chat-completion
    # calls the same day (see LESSONS_LEARNED.md), not just a models-list
    # check -- a model can be listed and still not actually serve.
    "nvidia": "meta/llama-3.3-70b-instruct",
    # ollama (local): "llama3" -- confirmed resolvable in Ollama's public
    # registry (200) but that's a different claim than "present on this
    # machine," and --verify-providers proved the gap: real call, 404
    # "model 'llama3' not found" (2026-09-22) -- it was never pulled
    # here. Unlike a hosted API's catalog, "ollama" is inherently
    # machine-dependent -- no hardcoded default can be universally right
    # -- but a default that's never actually been reachable on the
    # machine it was tested from is exactly the failure --verify-
    # providers exists to catch. Replaced with "llama3.2", confirmed
    # present (ollama list) and already used successfully this session.
    "ollama": "llama3.2",
    # ollama_cloud: "deepseek-v4-flash" was confirmed working 2026-08-18
    # (LESSONS_LEARNED.md #10-11) but as of 2026-08-25 returns 403 "this
    # model requires a subscription" on this account -- NOT verified to
    # still be a safe default, and NOT replaced, because every other
    # model tried on this account (gpt-oss:20b/120b, nemotron-3-nano:30b)
    # 403's with "payment past due" -- an account billing issue, not a
    # model-catalog one, so there's currently no way to verify a working
    # replacement. See LESSONS_LEARNED.md before assuming this default
    # works; resolve billing first, then re-verify.
    "ollama_cloud": "deepseek-v4-flash",
    # openai: not verified live (no OPENAI_API_KEY in this environment)
    # -- not known-deprecated, but unverified is not the same as verified.
    "openai": "gpt-4o-mini",
    # gemini: "gemini-2.5-flash" (picked from docs, never live-verified)
    # 404'd the moment it was actually called (2026-09-22) -- the API's own
    # error message: "no longer available to new users... use models/
    # gemini-3.6-flash". Replaced with that, confirmed via a real
    # successful chat-completion call the same day. Third default in this
    # file now proven wrong by an actual call after being picked from
    # docs/memory (nvidia's was the first, see above) -- reinforces: only
    # "confirmed via a real call" counts as verified here, docs-checked
    # is not enough.
    "gemini": "gemini-3.6-flash",
}


def _resolve_llm(cfg: dict):
    # gemini is the default extraction provider as of 2026-09-22 -- every
    # measurement before this point (Phase 2/3 figures in LESSONS_LEARNED.md,
    # the extraction-prompt tuning in content/extraction.py) used
    # ollama_cloud/deepseek-v4-flash. Numbers from a gemini-default run are
    # NOT directly comparable to those archived figures without re-running
    # the same check against gemini first -- see CLAUDE.md.
    provider_name = cfg.get("provider", "gemini")
    model_name = cfg.get("model") or _DEFAULT_MODEL_BY_PROVIDER[provider_name]
    llm, embeddings = get_llm(provider_name, model_name)
    return provider_name, model_name, llm, embeddings


async def headless_map(cfg: dict) -> dict:
    """Non-interactive equivalent of map_main() -- same worker wiring,
    same 'mapped' terminal status, just no Prompt.ask calls. Takes
    already-resolved seeds/host_prefix_pairs (see discover_command's
    docstring for why) rather than re-discovering. This is what
    --headless-config dispatches to for `"mode": "map"`; the server never
    imports or calls this directly -- it always goes through a subprocess
    (see server/jobs.py), same reasoning as discover_command's docstring.
    """
    run_dir = cfg["run_dir"]
    os.makedirs(run_dir, exist_ok=True)
    db_path = os.path.join(run_dir, "frontier.db")

    seeds = [(u, l) for u, l in cfg["seeds"]]
    host_prefix_pairs = [(h, p) for h, p in cfg["host_prefix_pairs"]]
    scope_check = _make_scope_check(host_prefix_pairs)

    frontier = Frontier(db_path, max_pages=cfg.get("max_pages"), max_depth=cfg.get("max_depth"))
    await frontier.open()
    await frontier.recover_crashed()
    await frontier.seed(seeds)

    robots_cache = RobotsCache(_http_get_text)
    crawl_workers_n = cfg.get("crawl_workers", 5)

    async with AsyncWebCrawler() as crawler:
        fetch_fn = _make_fetch_fn(crawler)
        content_queue = asyncio.Queue(maxsize=max(4, crawl_workers_n * 2))
        politeness = HostPoliteness(
            max_concurrent_per_host=config.MAX_CONCURRENT_REQUESTS_PER_HOST,
            default_delay_seconds=config.DEFAULT_POLITENESS_DELAY_SECONDS,
        )
        fatal_errors: list[str] = []
        tasks = [
            asyncio.create_task(
                crawl_worker(frontier, fetch_fn, content_queue, scope_check, robots_cache, politeness,
                             fatal_error_sink=fatal_errors)
            )
            for _ in range(crawl_workers_n)
        ] + [asyncio.create_task(map_worker(frontier, content_queue)) for _ in range(2)]

        await frontier.quiescent.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    counts = await frontier.counts_by_status()
    await frontier.close()
    return {"mode": "map", "counts_by_status": counts, "fatal_errors": fatal_errors}


async def headless_crawl(cfg: dict) -> dict:
    """Non-interactive equivalent of main() -- same worker wiring, same
    RAG/threshold/provider logic, just no Prompt.ask calls. See
    headless_map's docstring for the seeds/host_prefix_pairs contract and
    why this is always subprocess-launched, never called in-process by
    the server."""
    config.validate_system_one_config()
    run_dir = cfg["run_dir"]
    os.makedirs(run_dir, exist_ok=True)
    db_path = os.path.join(run_dir, "frontier.db")
    canonical_path = os.path.join(run_dir, "canonical.jsonl")

    seeds = [(u, l) for u, l in cfg["seeds"]]
    host_prefix_pairs = [(h, p) for h, p in cfg["host_prefix_pairs"]]
    scope_check = _make_scope_check(host_prefix_pairs)
    intent = cfg.get("intent") or None

    provider_name, model_name, llm, embeddings = _resolve_llm(cfg)

    frontier = Frontier(db_path, max_pages=cfg.get("max_pages"), max_depth=cfg.get("max_depth"))
    await frontier.open()
    await frontier.recover_crashed()
    await frontier.seed(seeds)

    embed_fn = _make_embed_fn(embeddings)
    intent_embedding = await embed_fn(intent) if intent else None
    score_fn = make_score_fn(intent, intent_embedding, embed_fn)
    extract_fn = _make_extract_fn(llm)
    extraction_units_fn = make_extraction_units_fn(
        config.EXTRACTION_STRATEGY, embed_fn=embed_fn, intent_embedding=intent_embedding,
    )
    robots_cache = RobotsCache(_http_get_text)

    quality_backend = make_quality_backend(_make_decision_chat_fn(llm)) if cfg.get("enable_quality_scoring") else None
    # None (unset): extract_worker looks up the per-method default itself
    # (config.DECISION_THRESHOLDS) once it knows which backend answered --
    # not resolved here, since this run's active method isn't necessarily
    # traditional.
    quality_threshold = cfg.get("quality_threshold")

    chunk_fn = None
    chroma_upsert_fn = None
    if cfg.get("build_rag"):
        import chromadb
        chroma_client = chromadb.PersistentClient(path=os.path.join(run_dir, "chroma_db"))
        collection = get_or_create_collection(
            chroma_client, config.CHROMA_COLLECTION_NAME, EMBEDDING_MODEL_NAME, EMBEDDING_DIM
        )
        chunk_store = ChunkStore(collection, embed_fn)

        def chunk_fn(content: str) -> list[dict]:
            pairs = split_into_parent_child_chunks(
                content, config.PARENT_CHUNK_SIZE, config.PARENT_CHUNK_OVERLAP,
                config.CHILD_CHUNK_SIZE, config.CHILD_CHUNK_OVERLAP,
            )
            return [{"text": child, "parent_text": parent} for child, parent in pairs]

        async def chroma_upsert_fn(url: str, chunks: list[dict]) -> None:
            for c in chunks:
                await chunk_store.add_or_merge_chunk(c["text"], url, c["parent_text"])

    writer = Writer(canonical_path, chroma_upsert_fn=chroma_upsert_fn)
    crawl_workers_n = cfg.get("crawl_workers", 5)
    extract_workers_n = cfg.get("extract_workers", 2)

    async with AsyncWebCrawler() as crawler:
        fetch_fn = _make_fetch_fn(crawler)
        content_queue = asyncio.Queue(maxsize=max(4, crawl_workers_n * 2))
        results_queue = asyncio.Queue(maxsize=max(4, extract_workers_n * 2))
        politeness = HostPoliteness(
            max_concurrent_per_host=config.MAX_CONCURRENT_REQUESTS_PER_HOST,
            default_delay_seconds=config.DEFAULT_POLITENESS_DELAY_SECONDS,
        )
        fatal_errors: list[str] = []
        tasks = [
            asyncio.create_task(
                crawl_worker(frontier, fetch_fn, content_queue, scope_check, robots_cache, politeness,
                             fatal_error_sink=fatal_errors)
            )
            for _ in range(crawl_workers_n)
        ] + [
            asyncio.create_task(
                extract_worker(frontier, content_queue, results_queue, score_fn, extract_fn,
                                cfg.get("extract_threshold", 0.0), cfg.get("follow_threshold", 0.0),
                                chunk_fn=chunk_fn, extraction_units_fn=extraction_units_fn,
                                generation_model=model_name, extraction_strategy=config.EXTRACTION_STRATEGY.value,
                                section_depth=config.SECTION_DEPTH, seed_prefixes=host_prefix_pairs,
                                quality_backend=quality_backend, quality_threshold=quality_threshold)
            )
            for _ in range(extract_workers_n)
        ] + [asyncio.create_task(writer_worker(frontier, results_queue, writer))]

        await frontier.quiescent.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    counts = await frontier.counts_by_status()
    await frontier.close()
    return {"mode": "crawl", "counts_by_status": counts, "fatal_errors": fatal_errors}


async def headless_run(cfg: dict) -> dict:
    if cfg["mode"] == "map":
        return await headless_map(cfg)
    if cfg["mode"] == "crawl":
        return await headless_crawl(cfg)
    raise ValueError(f"unknown headless mode: {cfg['mode']!r}")


async def map_main(root_url: str):
    """Map-only crawl: fetch pages, discover + scope-filter links, record
    them in the frontier -- no relevance scoring, no LLM extraction, no
    chunking, no embeddings, so the only real cost is fetching. Reuses the
    same frontier/crawl_worker/scope/politeness as a full crawl untouched;
    only the extract stage is replaced by map_worker (crawl/pipeline.py).

    Resumable into a full crawl by design: everything fetched here lands
    as 'mapped', a terminal status nothing auto-promotes out of. Running
    `--select`/`--select-prefix` against the same frontier.db requeues a
    chosen subtree to 'queued'; a subsequent `python main.py` run against
    the same root re-fetches (content from this pass was never persisted)
    and actually extracts just that subtree. Anything never selected stays
    'mapped' forever and is never touched again -- no need to explicitly
    cancel what you didn't select.
    """
    console.print(Panel.fit(f"[bold blue]Map-only crawl: {root_url}[/bold blue]"))
    if not root_url.startswith("http"):
        root_url = "https://" + root_url

    branches = await discover_branches(root_url)
    if not branches:
        console.print("[red]No branches discovered or discovery failed.[/red]")
        sys.exit(1)

    table = Table(title="Discovered Branches")
    table.add_column("ID", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("Count", justify="right", style="green")
    branch_list = list(branches.items())
    for i, (cat, urls) in enumerate(branch_list, 1):
        table.add_row(str(i), cat, str(len(urls)))
    console.print(table)

    branch_input = Prompt.ask("Enter comma-separated IDs of branches to map, or 'all'")
    selected_branches: list[tuple[str, list[str]]] = []
    if branch_input.strip().lower() == "all":
        selected_branches = branch_list
    else:
        try:
            ids = [int(x.strip()) for x in branch_input.split(",")]
        except ValueError:
            console.print("[red]Invalid input. Exiting.[/red]")
            sys.exit(1)
        for i in ids:
            if 1 <= i <= len(branch_list):
                selected_branches.append(branch_list[i - 1])
    if not selected_branches:
        console.print("[yellow]No branches selected. Exiting.[/yellow]")
        sys.exit(0)

    seeds: list[tuple[str, str | None]] = []
    host_prefix_pairs: list[tuple[str, str | None]] = []
    for category, urls in selected_branches:
        prefix_result = derive_prefix(urls)
        host_prefix_pairs.append((prefix_result.host, prefix_result.prefix))
        for url in urls:
            seeds.append((url, category))
    scope_check = _make_scope_check(host_prefix_pairs)

    max_pages_input = Prompt.ask("Max pages to fetch (blank = unlimited)", default="")
    max_pages = int(max_pages_input) if max_pages_input.strip() else None
    max_depth_input = Prompt.ask("Max crawl depth (blank = unlimited)", default="")
    max_depth = int(max_depth_input) if max_depth_input.strip() else None
    crawl_workers_n = int(Prompt.ask("Concurrent crawl workers", default="5"))

    os.makedirs(RUN_DIR, exist_ok=True)
    frontier = Frontier(FRONTIER_DB_PATH, max_pages=max_pages, max_depth=max_depth)
    await frontier.open()
    recovery = await frontier.recover_crashed()
    if recovery["requeued"] or recovery["failed"]:
        console.print(
            f"[yellow]Resumed from a prior run in {FRONTIER_DB_PATH}: "
            f"{recovery['requeued']} rows requeued, {recovery['failed']} marked failed.[/yellow]"
        )
    await frontier.seed(seeds)

    robots_cache = RobotsCache(_http_get_text)

    console.print(
        f"\n[bold green]Starting map-only crawl: {crawl_workers_n} crawl workers, "
        f"1 map worker -- no LLM calls, no relevance scoring[/bold green]"
    )

    started_at = time.monotonic()
    async with AsyncWebCrawler() as crawler:
        fetch_fn = _make_fetch_fn(crawler)
        content_queue = asyncio.Queue(maxsize=max(4, crawl_workers_n * 2))
        politeness = HostPoliteness(
            max_concurrent_per_host=config.MAX_CONCURRENT_REQUESTS_PER_HOST,
            default_delay_seconds=config.DEFAULT_POLITENESS_DELAY_SECONDS,
        )
        fatal_errors: list[str] = []
        tasks = [
            asyncio.create_task(
                crawl_worker(frontier, fetch_fn, content_queue, scope_check, robots_cache, politeness,
                             fatal_error_sink=fatal_errors)
            )
            for _ in range(crawl_workers_n)
        ] + [
            asyncio.create_task(map_worker(frontier, content_queue)),
            asyncio.create_task(run_progress_display(frontier)),
        ]
        await frontier.quiescent.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    elapsed_minutes = max((time.monotonic() - started_at) / 60, 1e-9)
    counts = await frontier.counts_by_status()
    await frontier.close()
    mapped = counts.get("mapped", 0)
    if fatal_errors:
        console.print(f"\n[bold red]Stopped early: {fatal_errors[0]}[/bold red]")
        console.print(f"[bold red]{counts}[/bold red]")
        sys.exit(1)
    console.print(f"\n[bold blue]Map complete.[/bold blue] {counts}")
    console.print(
        f"[dim]{mapped} pages fetched in {elapsed_minutes:.2f} min "
        f"({mapped / elapsed_minutes:.1f} pages/min) -- no LLM calls made.[/dim]"
    )
    console.print(
        f"[dim]Select a subtree for real extraction with `python main.py "
        f"--select-prefix <url-prefix>`, then rerun `python main.py` against "
        f"the same root -- selected pages re-fetch (content isn't persisted "
        f"from a map pass) and extract for real this time.[/dim]"
    )


async def cancel_command(db_path: str, url: str | None, prefix: str | None):
    if not os.path.exists(db_path):
        console.print(f"[red]No frontier DB found at {db_path}[/red]")
        sys.exit(1)
    frontier = Frontier(db_path)
    await frontier.open()
    cancelled = await frontier.cancel(url=url, prefix=prefix)
    await frontier.close()
    console.print(f"[yellow]Cancelled {len(cancelled)} row(s).[/yellow]")
    for u in cancelled[:50]:
        console.print(f"  - {u}")
    if len(cancelled) > 50:
        console.print(f"  ... and {len(cancelled) - 50} more")


async def select_command(db_path: str, url: str | None, prefix: str | None):
    if not os.path.exists(db_path):
        console.print(f"[red]No frontier DB found at {db_path}[/red]")
        sys.exit(1)
    frontier = Frontier(db_path)
    await frontier.open()
    requeued = await frontier.requeue_for_extraction(url=url, prefix=prefix)
    await frontier.close()
    console.print(f"[green]Selected {len(requeued)} mapped row(s) for extraction (requeued to 'queued').[/green]")
    for u in requeued[:50]:
        console.print(f"  - {u}")
    if len(requeued) > 50:
        console.print(f"  ... and {len(requeued) - 50} more")


def graph_command(db_path: str, graph_type: str, root: str | None, max_depth: int | None, out: str | None):
    if not os.path.exists(db_path):
        console.print(f"[red]No frontier DB found at {db_path}[/red]")
        sys.exit(1)
    started = time.perf_counter()
    if graph_type == "discovery":
        projection = discovery_graph(db_path, root_url=root, max_depth=max_depth)
    else:
        projection = path_tree(db_path, root_prefix=root, max_depth=max_depth)
    elapsed = time.perf_counter() - started
    payload = to_json(projection)
    console.print(
        f"[dim]Built {graph_type} projection in {elapsed:.3f}s -- "
        f"{projection['node_count']} nodes, {len(payload)} bytes JSON, "
        f"edges_tracked={projection['edges_tracked']}.[/dim]"
    )
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(payload)
        console.print(f"[green]Wrote {out}[/green]")
    else:
        print(payload)


async def suggest_selection_command(
    db_path: str, intent: str, provider: str, model: str | None, root_prefix: str | None, max_samples: int,
):
    """After a --map run, classify each discovered branch against `intent`
    -- one LLM call, prints JSON, proposes only. Nothing here selects
    anything; the printed prefixes are what you'd pass to --select-prefix
    yourself once you've read the reasons. See crawl/branch_select.py."""
    config.validate_system_one_config()
    if not os.path.exists(db_path):
        console.print(f"[red]No frontier DB found at {db_path}[/red]")
        sys.exit(1)
    projection = path_tree(db_path, root_prefix=root_prefix)
    model_name = model or _DEFAULT_MODEL_BY_PROVIDER[provider]
    llm, _ = get_llm(provider, model_name)
    classify_fn = _make_classify_fn(llm)
    branches = await suggest_branch_selection(projection["tree"], intent, classify_fn, max_samples=max_samples)
    print(json.dumps({"intent": intent, "provider": provider, "model": model_name, "branches": branches}, indent=2))


def live_status_command(db_path: str):
    if not os.path.exists(db_path):
        console.print(f"[red]No frontier DB found at {db_path}[/red]")
        sys.exit(1)
    print(json.dumps(read_live_status(db_path), indent=2))


async def verify_providers_command():
    """Pings every LLMProvider's default model (config._DEFAULT_MODEL_BY_
    PROVIDER) with a minimal real chat completion, one call each, and
    reports which actually respond -- the check that would have caught
    all four defaults this project has picked from docs and had proven
    wrong by an actual call (nvidia's 404, ollama_cloud's 403 subscription
    gate, gemini's 404, local-ollama's 404 -- see LESSONS_LEARNED.md).

    A provider with no key in .env is left out of the report entirely,
    not listed as "skipped" -- a provider nobody has signed up for isn't
    a gap to flag every run, it's just not in play; ollama (local) needs
    no key and is always attempted. No crawl, no site touched -- every
    call is a one-line ping to the provider's own chat endpoint."""
    console.print(Panel.fit("[bold blue]Verifying every configured provider's default model[/bold blue]"))
    rows = []
    for provider in config.LLMProvider:
        provider_name = provider.value
        model_name = _DEFAULT_MODEL_BY_PROVIDER[provider_name]
        llm_config = config.get_llm_config(provider)
        needs_key = provider != config.LLMProvider.OLLAMA
        if needs_key and not llm_config.api_key:
            continue
        try:
            llm, _ = get_llm(provider_name, model_name)
            response = await asyncio.to_thread(
                llm.invoke, [SystemMessage(content="Reply with exactly: OK"), HumanMessage(content="ping")],
            )
            rows.append((provider_name, model_name, "[green]responds[/green]", repr(response.content[:60])))
        except Exception as e:
            rows.append((provider_name, model_name, "[red]FAILED[/red]", f"{type(e).__name__}: {str(e)[:180]}"))

    if not rows:
        console.print("[yellow]No provider has a key set in .env -- nothing to verify.[/yellow]")
        return rows

    table = Table(title="Provider verification")
    table.add_column("provider")
    table.add_column("default model")
    table.add_column("status")
    table.add_column("detail")
    for provider_name, model_name, status, detail in rows:
        table.add_row(provider_name, model_name, status, detail)
    console.print(table)
    return rows


async def score_report_command(db_path: str):
    """Reads an existing frontier DB's recorded relevance scores and
    prints the distribution report -- run after a log-only (thresholds=0)
    pass to pick real thresholds from data."""
    if not os.path.exists(db_path):
        console.print(f"[red]No frontier DB found at {db_path}[/red]")
        sys.exit(1)
    frontier = Frontier(db_path)
    await frontier.open()
    scores = await frontier.all_scores()
    await frontier.close()
    console.print(format_score_report(scores))


async def dataset_report_command(canonical_path: str, frontier_db_path: str | None):
    """Reads canonical.jsonl (and, if given, the frontier DB it came from)
    and reports pair-level redundancy stats -- the instrument for
    ROADMAP #23. Offline, no LLM calls -- pure reads over what a
    completed run already wrote to disk."""
    if not os.path.exists(canonical_path):
        console.print(f"[red]No canonical file found at {canonical_path}[/red]")
        sys.exit(1)
    records = load_canonical_records(canonical_path)

    scores = None
    if frontier_db_path:
        if not os.path.exists(frontier_db_path):
            console.print(f"[yellow]No frontier DB found at {frontier_db_path} -- skipping the scores section.[/yellow]")
        else:
            frontier = Frontier(frontier_db_path)
            await frontier.open()
            scores = await frontier.all_scores()
            await frontier.close()

    console.print(build_dataset_report(records, scores=scores))


async def query_command(question: str, chroma_dir: str | None = None):
    """Embeds the question, searches the persisted Chroma index, prints
    each result's parent text and sources -- the proof the RAG index
    stops being write-only (ROADMAP.md #3). chroma_dir defaults to the
    single-run CHROMA_PERSIST_DIR for backward compatibility; the server
    passes a specific run's own chroma_db explicitly (data/run-*/ can
    hold many runs, each with its own index) -- see server/app.py."""
    import chromadb
    from llm_factory import LocalOllamaEmbeddings

    chroma_dir = chroma_dir or config.CHROMA_PERSIST_DIR
    embeddings = LocalOllamaEmbeddings(model_name=EMBEDDING_MODEL_NAME)
    embed_fn = _make_embed_fn(embeddings)

    client = chromadb.PersistentClient(path=chroma_dir)
    try:
        collection = client.get_collection(config.CHROMA_COLLECTION_NAME)
    except Exception:
        console.print(
            f"[red]No collection {config.CHROMA_COLLECTION_NAME!r} found in "
            f"{chroma_dir} -- run a crawl with RAG enabled first.[/red]"
        )
        sys.exit(1)

    results = await query_chunks(collection, embed_fn, question, EMBEDDING_MODEL_NAME, EMBEDDING_DIM)
    console.print(format_query_results(results))


async def _http_get_text(url: str) -> str | None:
    def _get():
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": HTTP_USER_AGENT})
            return resp.text if resp.status_code == 200 else None
        except Exception:
            return None
    return await asyncio.to_thread(_get)


def _make_fetch_fn(crawler: AsyncWebCrawler):
    """Shares one AsyncWebCrawler instance across every crawl_worker call
    -- matching orchestrator.py's existing pattern, not spinning up a new
    browser context per fetch. Live crawl bypasses crawl4ai's cache
    (unlike dry_run/discovery, which deliberately reuse it).

    Chrome-stripping (chrome_strip.py, steps 4/9/11) was built and
    validated against cached HTML fixtures but never actually wired in
    here until step 8 Part D's real crawl exposed the gap (nav-menu
    questions in real extraction output -- see LESSONS_LEARNED.md).
    excluded_tags/excluded_selector go through CrawlerRunConfig directly
    (crawl4ai's own HTML-cleaning stage, the mechanism chrome_strip.py's
    own docstring says this was always meant to use) rather than
    re-running clean_html()+DefaultMarkdownGenerator() a second time on
    already-converted markdown. normalize_link_text() (layer 2, markdown
    link-syntax reduction) and strip_text_patterns() (layer 3, UI text
    that survives structural exclusion) still run on the resulting
    markdown, same as chrome_strip.strip_chrome()'s own pipeline."""
    async def fetch_fn(url: str) -> tuple[str, list[str]]:
        run_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            excluded_tags=DEFAULT_EXCLUDED_TAGS,
            excluded_selector=DEFAULT_EXCLUDED_SELECTOR,
        )
        result = await crawler.arun(url=url, config=run_config)
        if not result.success:
            status = result.redirected_status_code or result.status_code
            if status and status != 200:
                raise FetchHTTPError(status)
            error_message = result.error_message or "fetch failed"
            # crawl4ai catches Playwright's own exceptions internally and
            # returns them as result.error_message rather than raising --
            # this substring is the one shared-browser-is-dead signature
            # found causing 90 identical failures in a real run (see
            # LESSONS_LEARNED.md). Distinguished from an ordinary
            # FetchTimeout because retrying THIS specific failure against
            # the same shared browser is a guaranteed repeat, not a
            # transient one -- crawl_worker fails the whole run fast on it
            # instead of retrying into a browser that's already dead.
            if "Connection closed while reading from the driver" in error_message:
                raise BrowserDriverError(error_message)
            raise FetchTimeout(error_message)
        links = result.links.get("internal", []) + result.links.get("external", [])
        raw_hrefs = [l.get("href", "") for l in links if l.get("href")]
        markdown = strip_text_patterns(normalize_link_text(result.markdown))
        return markdown, raw_hrefs
    return fetch_fn


def _make_embed_fn(embeddings):
    async def embed_fn(text: str) -> list[float]:
        return await asyncio.to_thread(embeddings.embed_query, text)
    return embed_fn


def _is_rate_limit_exception(e: Exception) -> bool:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    return status == 429


_EXTRACTION_SYSTEM_PROMPTS = {
    config.ExtractionPromptVariant.STANDARD: QA_EXTRACTION_SYSTEM_PROMPT,
    config.ExtractionPromptVariant.CODE_FOCUSED: CODE_QA_EXTRACTION_SYSTEM_PROMPT,
}


def _make_extract_fn(llm, prompt_variant: "config.ExtractionPromptVariant" = None):
    system_prompt = _EXTRACTION_SYSTEM_PROMPTS[prompt_variant or config.EXTRACTION_PROMPT_VARIANT]

    async def extract_fn(content: str) -> str:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"Text to process:\n\n{content[:config.MAX_EXTRACT_CHARS]}"),
        ]
        try:
            response = await asyncio.to_thread(llm.invoke, messages)
        except Exception as e:
            if _is_rate_limit_exception(e):
                raise RateLimitError() from e
            raise
        return response.content
    return extract_fn


def _make_decision_chat_fn(llm):
    """The (system_prompt, user_prompt) -> text shape decisions.
    LLMTraditionalBackend needs -- unlike extract_fn/classify_fn, the
    system prompt isn't fixed at construction time, since this one chat_fn
    is shared by every decision point that ends up using the generic
    traditional backend (today: sample quality scoring only -- see
    content/sample_quality.py)."""
    async def chat_fn(system_prompt: str, user_prompt: str) -> str:
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        try:
            response = await asyncio.to_thread(llm.invoke, messages)
        except Exception as e:
            if _is_rate_limit_exception(e):
                raise RateLimitError() from e
            raise
        return response.content
    return chat_fn


def _make_classify_fn(llm):
    async def classify_fn(prompt: str) -> str:
        messages = [
            SystemMessage(content=BRANCH_CLASSIFICATION_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        try:
            response = await asyncio.to_thread(llm.invoke, messages)
        except Exception as e:
            if _is_rate_limit_exception(e):
                raise RateLimitError() from e
            raise
        return response.content
    return classify_fn


def _make_scope_check(host_prefix_pairs: list[tuple[str, str | None]]):
    def scope_check(url: str) -> bool:
        return any(
            is_in_scope(url, host=host, prefix=prefix).allowed
            for host, prefix in host_prefix_pairs
        )
    return scope_check


async def main():
    config.validate_system_one_config()
    console.print(Panel.fit("[bold blue]Prompt-Steered Crawler[/bold blue]"))

    root_url = Prompt.ask("Enter the Root URL to start discovery")
    if not root_url.startswith("http"):
        root_url = "https://" + root_url

    intent = Prompt.ask(
        "Describe what you want from this site "
        "(leave blank to scrape everything, no relevance filtering)",
        default="",
    ).strip() or None
    if intent is None:
        console.print("[yellow]No intent given -- relevance gate is off, every fetched page extracts.[/yellow]")

    branches = await discover_branches(root_url)
    if not branches:
        console.print("[red]No branches discovered or discovery failed.[/red]")
        sys.exit(1)

    table = Table(title="Discovered Branches")
    table.add_column("ID", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("Count", justify="right", style="green")
    branch_list = list(branches.items())
    for i, (cat, urls) in enumerate(branch_list, 1):
        table.add_row(str(i), cat, str(len(urls)))
    console.print(table)

    branch_input = Prompt.ask("Enter comma-separated IDs of branches to crawl, or 'all'")
    selected_branches: list[tuple[str, list[str]]] = []
    if branch_input.strip().lower() == "all":
        selected_branches = branch_list
    else:
        try:
            ids = [int(x.strip()) for x in branch_input.split(",")]
        except ValueError:
            console.print("[red]Invalid input. Exiting.[/red]")
            sys.exit(1)
        for i in ids:
            if 1 <= i <= len(branch_list):
                selected_branches.append(branch_list[i - 1])

    if not selected_branches:
        console.print("[yellow]No branches selected. Exiting.[/yellow]")
        sys.exit(0)

    seeds: list[tuple[str, str | None]] = []
    host_prefix_pairs: list[tuple[str, str | None]] = []
    for category, urls in selected_branches:
        prefix_result = derive_prefix(urls)
        host_prefix_pairs.append((prefix_result.host, prefix_result.prefix))
        for url in urls:
            seeds.append((url, category))
    scope_check = _make_scope_check(host_prefix_pairs)

    console.print("\n[bold]Select LLM Provider (chat/extraction)[/bold]")
    console.print("1. NVIDIA NIM")
    console.print("2. Ollama (local)")
    console.print("3. Ollama (cloud)")
    console.print("4. OpenAI")
    console.print("5. Gemini")
    llm_choice = Prompt.ask("Choice", choices=["1", "2", "3", "4", "5"], default="5")
    provider_map = {"1": "nvidia", "2": "ollama", "3": "ollama_cloud", "4": "openai", "5": "gemini"}
    provider_name = provider_map[llm_choice]
    model_name = Prompt.ask(f"Enter model name for {provider_name}", default=_DEFAULT_MODEL_BY_PROVIDER[provider_name])
    llm, embeddings = get_llm(provider_name, model_name)
    console.print(
        "[dim]Embeddings always use local Ollama (nomic-embed-text) regardless of the "
        "chat provider chosen -- see CLAUDE.md's provider-routing invariant.[/dim]"
    )

    console.print("\n[bold]Output[/bold]")
    build_rag = Confirm.ask("Build Vector RAG index (Chroma)?", default=False)
    enable_quality_scoring = Confirm.ask(
        "Score each extracted pair's quality and drop low-quality ones? "
        "(opt-in -- adds one decision call per pair; traditional path is an LLM call, see SYSTEM_ONE_MODELS.md)",
        default=False,
    )
    quality_threshold_input = (
        Prompt.ask(
            "Quality threshold (blank = use the calibrated default for whichever "
            "method answers -- traditional/jev/laya each score on a different "
            "scale, see config.DECISION_THRESHOLDS; 0 = keep everything, log-only)",
            default="",
        ) if enable_quality_scoring else None
    )
    quality_threshold = float(quality_threshold_input) if quality_threshold_input else None
    console.print(
        "[dim]split_jsonl (by section) isn't wired into the new pipeline yet -- "
        "unified JSONL only. Old orchestrator.py flow still has it if needed.[/dim]"
    )

    max_pages_input = Prompt.ask("Max pages to fetch (blank = unlimited)", default="")
    max_pages = int(max_pages_input) if max_pages_input.strip() else None
    max_depth_input = Prompt.ask("Max crawl depth (blank = unlimited)", default="")
    max_depth = int(max_depth_input) if max_depth_input.strip() else None

    console.print(
        "\n[bold yellow]Both relevance thresholds default to 0 -- log-only by "
        "construction: nothing gets skipped, every page's score is recorded. "
        f"Run `python main.py --score-report {FRONTIER_DB_PATH}` after this "
        "completes to pick real thresholds from the actual distribution, "
        "rather than guessing before you've seen real data.[/bold yellow]"
    )
    extract_threshold = float(Prompt.ask("Extraction relevance threshold", default="0"))
    follow_threshold = float(Prompt.ask("Follow relevance threshold", default="0"))

    crawl_workers_n = int(Prompt.ask("Concurrent crawl workers", default="5"))
    extract_workers_n = int(Prompt.ask("Concurrent extract workers", default="2"))

    os.makedirs(RUN_DIR, exist_ok=True)
    frontier = Frontier(FRONTIER_DB_PATH, max_pages=max_pages, max_depth=max_depth)
    await frontier.open()
    recovery = await frontier.recover_crashed()
    if recovery["requeued"] or recovery["failed"]:
        console.print(
            f"[yellow]Resumed from a prior run in {FRONTIER_DB_PATH}: "
            f"{recovery['requeued']} rows requeued, {recovery['failed']} marked failed.[/yellow]"
        )
    await frontier.seed(seeds)

    embed_fn = _make_embed_fn(embeddings)
    intent_embedding = await embed_fn(intent) if intent else None
    score_fn = make_score_fn(intent, intent_embedding, embed_fn)
    extract_fn = _make_extract_fn(llm)
    extraction_units_fn = make_extraction_units_fn(
        config.EXTRACTION_STRATEGY, embed_fn=embed_fn, intent_embedding=intent_embedding,
    )
    quality_backend = make_quality_backend(_make_decision_chat_fn(llm)) if enable_quality_scoring else None
    robots_cache = RobotsCache(_http_get_text)

    chunk_fn = None
    chroma_upsert_fn = None
    if build_rag:
        import chromadb
        chroma_client = chromadb.PersistentClient(path=config.CHROMA_PERSIST_DIR)
        collection = get_or_create_collection(
            chroma_client, config.CHROMA_COLLECTION_NAME, EMBEDDING_MODEL_NAME, EMBEDDING_DIM
        )
        chunk_store = ChunkStore(collection, embed_fn)

        def chunk_fn(content: str) -> list[dict]:
            pairs = split_into_parent_child_chunks(
                content, config.PARENT_CHUNK_SIZE, config.PARENT_CHUNK_OVERLAP,
                config.CHILD_CHUNK_SIZE, config.CHILD_CHUNK_OVERLAP,
            )
            return [{"text": child, "parent_text": parent} for child, parent in pairs]

        async def chroma_upsert_fn(url: str, chunks: list[dict]) -> None:
            for c in chunks:
                await chunk_store.add_or_merge_chunk(c["text"], url, c["parent_text"])

    writer = Writer(CANONICAL_PATH, chroma_upsert_fn=chroma_upsert_fn)

    console.print(f"\n[bold green]Starting: {crawl_workers_n} crawl workers, "
                  f"{extract_workers_n} extract workers, 1 writer[/bold green]")

    async with AsyncWebCrawler() as crawler:
        fetch_fn = _make_fetch_fn(crawler)
        content_queue = asyncio.Queue(maxsize=max(4, crawl_workers_n * 2))
        results_queue = asyncio.Queue(maxsize=max(4, extract_workers_n * 2))

        # One shared instance across every crawl_worker task -- per-host
        # state (semaphore, last-request timestamp) has to be shared for
        # the throttling to apply across workers, not reset per worker.
        politeness = HostPoliteness(
            max_concurrent_per_host=config.MAX_CONCURRENT_REQUESTS_PER_HOST,
            default_delay_seconds=config.DEFAULT_POLITENESS_DELAY_SECONDS,
        )

        fatal_errors: list[str] = []
        tasks = [
            asyncio.create_task(
                crawl_worker(frontier, fetch_fn, content_queue, scope_check, robots_cache, politeness,
                             fatal_error_sink=fatal_errors)
            )
            for _ in range(crawl_workers_n)
        ] + [
            asyncio.create_task(
                extract_worker(frontier, content_queue, results_queue, score_fn, extract_fn,
                                extract_threshold, follow_threshold, chunk_fn=chunk_fn,
                                extraction_units_fn=extraction_units_fn,
                                generation_model=model_name,
                                extraction_strategy=config.EXTRACTION_STRATEGY.value,
                                section_depth=config.SECTION_DEPTH,
                                seed_prefixes=host_prefix_pairs,
                                quality_backend=quality_backend, quality_threshold=quality_threshold)
            )
            for _ in range(extract_workers_n)
        ] + [
            asyncio.create_task(writer_worker(frontier, results_queue, writer)),
            asyncio.create_task(run_progress_display(frontier)),
        ]

        await frontier.quiescent.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    counts = await frontier.counts_by_status()
    await frontier.close()
    if fatal_errors:
        console.print(f"\n[bold red]Stopped early: {fatal_errors[0]}[/bold red]")
        console.print(f"[bold red]{counts}[/bold red]")
        console.print(
            "[dim]The frontier DB reflects a partial run -- resuming it will "
            "retry the URLs left in_progress/queued, but won't undo the "
            "underlying cause. Check LESSONS_LEARNED.md before resuming.[/dim]"
        )
        sys.exit(1)
    console.print(f"\n[bold blue]Done.[/bold blue] {counts}")
    console.print(f"[dim]Run `python main.py --score-report {FRONTIER_DB_PATH}` to see the relevance score distribution.[/dim]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        metavar="URL",
        help="Fetch URL, derive scope per discovered branch, print accept/"
             "reject for every discovered href. No crawl, no LLM calls.",
    )
    parser.add_argument(
        "--score-report",
        metavar="FRONTIER_DB",
        help="Print the relevance score distribution recorded in an existing "
             "frontier DB (from a prior run) -- no crawl, no LLM calls.",
    )
    parser.add_argument(
        "--verify-providers",
        action="store_true",
        help="Ping every LLMProvider's default model (one real chat call "
             "each, only for providers with a key set in .env) and report "
             "which actually respond -- run this before trusting any "
             "default picked from docs, not after. No crawl.",
    )
    parser.add_argument(
        "--query",
        metavar="QUESTION",
        help="Embed QUESTION, search the persisted Chroma index "
             f"({config.CHROMA_PERSIST_DIR} unless --query-chroma-dir is "
             "given), print each result's parent text and sources. "
             "Requires a prior run with RAG enabled. One live embedding call.",
    )
    parser.add_argument(
        "--query-chroma-dir",
        metavar="DIR",
        default=None,
        help="Chroma persist dir to search (default: the single-run "
             f"{config.CHROMA_PERSIST_DIR}) -- e.g. data/run-X/chroma_db "
             "for a specific named run.",
    )
    parser.add_argument(
        "--dataset-report",
        metavar="CANONICAL_JSONL",
        help="Report pair counts, exact/near-duplicate questions and answers, "
             "and redundancy by cause (adjacent-chunk overlap vs. same-chunk "
             "paraphrase padding) for a completed run's canonical file. "
             "Offline, no LLM calls. Pair with --dataset-report-frontier-db "
             "to also show relevance scores against what was extracted.",
    )
    parser.add_argument(
        "--dataset-report-frontier-db",
        metavar="FRONTIER_DB",
        default=None,
        help="Optional frontier DB to cross-reference with --dataset-report.",
    )
    parser.add_argument(
        "--map",
        metavar="URL",
        help="Map-only crawl: fetch + discover links + record the frontier, "
             "no relevance scoring, no LLM extraction, no chunking, no "
             "embeddings. Interactive branch selection, same as a full crawl "
             "-- writes to the same frontier.db, so it's resumable into a "
             "full crawl via --select/--select-prefix afterward.",
    )
    parser.add_argument(
        "--cancel",
        metavar="URL",
        help="Cancel this URL and every descendant reachable through "
             "parent_url (one transaction). Pairs with --cancel-db.",
    )
    parser.add_argument(
        "--cancel-prefix",
        metavar="PREFIX",
        help="Cancel every URL under this prefix, and their descendants. "
             "Pairs with --cancel-db.",
    )
    parser.add_argument(
        "--cancel-db",
        metavar="FRONTIER_DB",
        default=FRONTIER_DB_PATH,
        help="Frontier DB to cancel against (default: %(default)s).",
    )
    parser.add_argument(
        "--select",
        metavar="URL",
        help="Promote a previously-mapped URL (and its mapped descendants) "
             "back to 'queued', so the next crawl run against this frontier "
             "actually re-fetches and extracts it. Pairs with --select-db.",
    )
    parser.add_argument(
        "--select-prefix",
        metavar="PREFIX",
        help="Same as --select, but every mapped URL under this prefix. "
             "Pairs with --select-db.",
    )
    parser.add_argument(
        "--select-db",
        metavar="FRONTIER_DB",
        default=FRONTIER_DB_PATH,
        help="Frontier DB to select against (default: %(default)s).",
    )
    parser.add_argument(
        "--graph",
        choices=["discovery", "path-tree"],
        help="Print a JSON graph projection over a frontier DB -- "
             "'discovery' (edges = parent_url, the first-discoverer shape) "
             "or 'path-tree' (grouped by URL path segments). Offline, no "
             "crawl, no LLM calls. Pairs with --graph-db/--graph-root/"
             "--graph-max-depth/--graph-out.",
    )
    parser.add_argument("--graph-db", metavar="FRONTIER_DB", default=FRONTIER_DB_PATH,
                         help="Frontier DB to read (default: %(default)s).")
    parser.add_argument(
        "--graph-root",
        metavar="URL_OR_PREFIX",
        default=None,
        help="Scope to this node's subtree (discovery graph) or this path "
             "prefix (path tree) instead of dumping the whole DB.",
    )
    parser.add_argument(
        "--graph-max-depth",
        type=int,
        default=None,
        help="With --graph-root, how many hops (discovery) or path segments "
             "(path-tree) deep to return. subtree_size/child_count on the "
             "boundary nodes still reflect the true, unpruned subtree.",
    )
    parser.add_argument("--graph-out", metavar="PATH", default=None,
                         help="Write the JSON here instead of stdout.")
    parser.add_argument(
        "--suggest-select",
        metavar="INTENT",
        help="After a --map run, classify each discovered branch (select/"
             "reject/uncertain, one reason each) against INTENT -- one "
             "LLM call, prints JSON. Proposes only: nothing is selected "
             "until you run --select/--select-prefix yourself. Pairs "
             "with --suggest-select-db/-root/-provider/-model/-samples.",
    )
    parser.add_argument("--suggest-select-db", metavar="FRONTIER_DB", default=FRONTIER_DB_PATH,
                         help="Frontier DB to read (default: %(default)s).")
    parser.add_argument(
        "--suggest-select-root",
        metavar="PREFIX",
        default=None,
        help="Scope to this path prefix instead of auto-detecting the "
             "crawl's effective root (the first point the site's path "
             "tree actually forks).",
    )
    parser.add_argument("--suggest-select-provider", default="openai",
                         help="LLM provider for classification (default: %(default)s).")
    parser.add_argument("--suggest-select-model", default=None,
                         help="LLM model for classification (default: the provider's usual default).")
    parser.add_argument("--suggest-select-samples", type=int, default=5,
                         help="Sample URLs to show the model per branch (default: %(default)s).")
    parser.add_argument(
        "--live-status",
        action="store_true",
        help="Print one JSON snapshot of a crawl's current state -- counts "
             "by status, in-flight URLs (with parent + elapsed seconds), "
             "recently completed, and the last-change timestamp. Read-only, "
             "safe to run against a frontier.db a crawl is actively writing "
             "to. Poll by re-running. Pairs with --live-status-db.",
    )
    parser.add_argument("--live-status-db", metavar="FRONTIER_DB", default=FRONTIER_DB_PATH,
                         help="Frontier DB to read (default: %(default)s).")
    parser.add_argument(
        "--discover-json",
        metavar="URL",
        help="Fetch URL, derive scope per discovered branch, print the "
             "result as JSON (category, urls, host, prefix) instead of "
             "--dry-run's human-readable accept/reject listing. One "
             "bounded page-fetch, no LLM calls -- what the server's "
             "discover endpoint runs as a subprocess.",
    )
    parser.add_argument(
        "--headless-config",
        metavar="CONFIG_JSON",
        help="Run a map or full crawl non-interactively from a resolved "
             "JSON config file (mode, run_dir, seeds, host_prefix_pairs, "
             "and the same tunables the interactive prompts ask for) -- "
             "no stdin prompts, so this is what the server launches as a "
             "subprocess for both starting and resuming a run. Prints the "
             "final counts_by_status as JSON on completion.",
    )
    parser.add_argument("--pause", metavar="FRONTIER_DB",
                         help="Stop a running crawl/map from claiming new work; in-flight pages finish normally.")
    parser.add_argument("--unpause", metavar="FRONTIER_DB",
                         help="Undo --pause -- claiming resumes on the next poll, no restart needed.")
    args = parser.parse_args()

    try:
        if args.dry_run:
            asyncio.run(dry_run(args.dry_run))
        elif args.discover_json:
            print(json.dumps(asyncio.run(discover_command(args.discover_json))))
        elif args.headless_config:
            with open(args.headless_config, encoding="utf-8") as f:
                cfg = json.load(f)
            result = asyncio.run(headless_run(cfg))
            print(json.dumps(result))
            if result.get("fatal_errors"):
                sys.exit(1)
        elif args.pause:
            async def _pause():
                f = Frontier(args.pause)
                await f.open()
                await f.set_paused(True)
                await f.close()
            asyncio.run(_pause())
            console.print(f"[yellow]Paused {args.pause}[/yellow]")
        elif args.unpause:
            async def _unpause():
                f = Frontier(args.unpause)
                await f.open()
                await f.set_paused(False)
                await f.close()
            asyncio.run(_unpause())
            console.print(f"[green]Unpaused {args.unpause}[/green]")
        elif args.map:
            asyncio.run(map_main(args.map))
        elif args.cancel or args.cancel_prefix:
            if args.cancel and args.cancel_prefix:
                console.print("[red]Use only one of --cancel / --cancel-prefix.[/red]")
                sys.exit(1)
            asyncio.run(cancel_command(args.cancel_db, url=args.cancel, prefix=args.cancel_prefix))
        elif args.select or args.select_prefix:
            if args.select and args.select_prefix:
                console.print("[red]Use only one of --select / --select-prefix.[/red]")
                sys.exit(1)
            asyncio.run(select_command(args.select_db, url=args.select, prefix=args.select_prefix))
        elif args.graph:
            graph_command(args.graph_db, args.graph, args.graph_root, args.graph_max_depth, args.graph_out)
        elif args.suggest_select:
            asyncio.run(suggest_selection_command(
                args.suggest_select_db, args.suggest_select, args.suggest_select_provider,
                args.suggest_select_model, args.suggest_select_root, args.suggest_select_samples,
            ))
        elif args.live_status:
            live_status_command(args.live_status_db)
        elif args.score_report:
            asyncio.run(score_report_command(args.score_report))
        elif args.verify_providers:
            asyncio.run(verify_providers_command())
        elif args.query:
            asyncio.run(query_command(args.query, chroma_dir=args.query_chroma_dir))
        elif args.dataset_report:
            asyncio.run(dataset_report_command(args.dataset_report, args.dataset_report_frontier_db))
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
