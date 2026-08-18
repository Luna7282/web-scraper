import argparse
import asyncio
import os
import sys

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
from crawl.pipeline import crawl_worker, extract_worker, writer_worker, FetchTimeout, FetchHTTPError, RateLimitError
from content.extraction import QA_EXTRACTION_SYSTEM_PROMPT
from content.relevance import make_score_fn
from crawl.robots_cache import RobotsCache
from storage.writer import Writer
from progress_display import run_progress_display
from crawl.score_report import format_score_report
from storage.chunk_store import ChunkStore, get_or_create_collection, split_into_parent_child_chunks
from storage.query import query_chunks, format_query_results
from content.extraction_units import make_extraction_units_fn
from export.dataset_report import build_dataset_report
from export.export import load_canonical_records
from content.chrome_strip import DEFAULT_EXCLUDED_SELECTOR, DEFAULT_EXCLUDED_TAGS, strip_text_patterns

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


async def query_command(question: str):
    """Embeds the question, searches the persisted Chroma index, prints
    each result's parent text and sources -- the proof the RAG index
    stops being write-only (ROADMAP.md #3)."""
    import chromadb
    from llm_factory import LocalOllamaEmbeddings

    embeddings = LocalOllamaEmbeddings(model_name=EMBEDDING_MODEL_NAME)
    embed_fn = _make_embed_fn(embeddings)

    client = chromadb.PersistentClient(path=config.CHROMA_PERSIST_DIR)
    try:
        collection = client.get_collection(config.CHROMA_COLLECTION_NAME)
    except Exception:
        console.print(
            f"[red]No collection {config.CHROMA_COLLECTION_NAME!r} found in "
            f"{config.CHROMA_PERSIST_DIR} -- run a crawl with RAG enabled first.[/red]"
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
    already-converted markdown. strip_text_patterns() (layer 2, UI text
    that survives structural exclusion) still runs on the resulting
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
            raise FetchTimeout(result.error_message or "fetch failed")
        links = result.links.get("internal", []) + result.links.get("external", [])
        raw_hrefs = [l.get("href", "") for l in links if l.get("href")]
        markdown = strip_text_patterns(result.markdown)
        return markdown, raw_hrefs
    return fetch_fn


def _make_embed_fn(embeddings):
    async def embed_fn(text: str) -> list[float]:
        return await asyncio.to_thread(embeddings.embed_query, text)
    return embed_fn


def _is_rate_limit_exception(e: Exception) -> bool:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    return status == 429


def _make_extract_fn(llm):
    async def extract_fn(content: str) -> str:
        messages = [
            SystemMessage(content=QA_EXTRACTION_SYSTEM_PROMPT),
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


def _make_scope_check(host_prefix_pairs: list[tuple[str, str | None]]):
    def scope_check(url: str) -> bool:
        return any(
            is_in_scope(url, host=host, prefix=prefix).allowed
            for host, prefix in host_prefix_pairs
        )
    return scope_check


async def main():
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
    llm_choice = Prompt.ask("Choice", choices=["1", "2", "3", "4"], default="4")
    provider_map = {"1": "nvidia", "2": "ollama", "3": "ollama_cloud", "4": "openai"}
    provider_name = provider_map[llm_choice]
    default_models = {
        "nvidia": "meta/llama3-70b-instruct",
        "ollama": "llama3",
        "ollama_cloud": "deepseek-v4-flash",
        "openai": "gpt-4o-mini",
    }
    model_name = Prompt.ask(f"Enter model name for {provider_name}", default=default_models[provider_name])
    llm, embeddings = get_llm(provider_name, model_name)
    console.print(
        "[dim]Embeddings always use local Ollama (nomic-embed-text) regardless of the "
        "chat provider chosen -- see CLAUDE.md's provider-routing invariant.[/dim]"
    )

    console.print("\n[bold]Output[/bold]")
    build_rag = Confirm.ask("Build Vector RAG index (Chroma)?", default=False)
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
    score_fn = make_score_fn(intent_embedding, embed_fn)
    extract_fn = _make_extract_fn(llm)
    extraction_units_fn = make_extraction_units_fn(
        config.EXTRACTION_STRATEGY, embed_fn=embed_fn, intent_embedding=intent_embedding,
    )
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

        tasks = [
            asyncio.create_task(crawl_worker(frontier, fetch_fn, content_queue, scope_check, robots_cache))
            for _ in range(crawl_workers_n)
        ] + [
            asyncio.create_task(
                extract_worker(frontier, content_queue, results_queue, score_fn, extract_fn,
                                extract_threshold, follow_threshold, chunk_fn=chunk_fn,
                                extraction_units_fn=extraction_units_fn,
                                generation_model=model_name,
                                extraction_strategy=config.EXTRACTION_STRATEGY.value,
                                section_depth=config.SECTION_DEPTH)
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
        "--query",
        metavar="QUESTION",
        help="Embed QUESTION, search the persisted Chroma index "
             f"({config.CHROMA_PERSIST_DIR}), print each result's parent text "
             "and sources. Requires a prior run with RAG enabled. One live "
             "embedding call.",
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
    args = parser.parse_args()

    try:
        if args.dry_run:
            asyncio.run(dry_run(args.dry_run))
        elif args.score_report:
            asyncio.run(score_report_command(args.score_report))
        elif args.query:
            asyncio.run(query_command(args.query))
        elif args.dataset_report:
            asyncio.run(dataset_report_command(args.dataset_report, args.dataset_report_frontier_db))
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
