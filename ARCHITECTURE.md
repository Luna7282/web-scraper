# Architecture — Current State

Verified by reading every source file (532 lines across 7 modules) and inspecting
the artifacts on disk. Not a design doc — this is what the code actually does as
of 2026-08-18.

## What this is

A single-run, interactive CLI tool. You run `python main.py`, answer prompts
(root URL, which discovered branches to crawl, which LLM provider, output
formats, concurrency), and it crawls, extracts Q&A pairs via an LLM, and
optionally writes vectors to a local ChromaDB. There is no server, no
scheduler, no resume, no query interface — it's a one-shot batch script with a
`rich` prompt UI on top.

## Module map

| File | Lines | Role |
|---|---|---|
| `main.py` | 115 | Interactive CLI entrypoint. Prompts for URL, branch selection, LLM provider/model, output prefs, concurrency. Wires everything together and calls `start_workers`. |
| `discovery.py` | 45 | `discover_branches()` — one `crawler.arun()` on the root URL, buckets its links into categories (`External: {domain}` / `Path: /{first-segment}/*` / `Root Level`). |
| `orchestrator.py` | 82 | `worker()` + `start_workers()` — asyncio.Queue-based crawl loop. N workers pull URLs, fetch+extract markdown via crawl4ai, hand markdown to `OutputManager.process_page`, and enqueue newly discovered links. |
| `output_manager.py` | 175 | `OutputManager` — per-page LLM Q&A extraction → JSONL write, and per-page Parent-Child chunking → Chroma embed. Owns both output paths. |
| `llm_factory.py` | 48 | `get_llm()` — builds a `ChatOpenAI` client pointed at whichever provider's base_url/key, plus a hand-written `LocalOllamaEmbeddings` HTTP client for `nomic-embed-text` on `localhost:11434`. |
| `config.py` | 38 | `LLMProvider` enum, `get_llm_config()` (env-var → base_url/key mapping), `OutputPreferences` pydantic model. |
| `duplicate.py` | 29 | **Standalone**, not imported by anything else. Manual exact-match dedup script for `data/unified.jsonl` and `data/awesome.jsonl` only. Run by hand (`python duplicate.py`). Internal comment still says `# deduplicate.py` — a leftover from a rename. |

No `main.py`-adjacent test files, no `logging` module usage anywhere (all
`print()`), no `.gitignore` / no git repo initialized at all.

## Real end-to-end call chain

**Discovery → branch table**
`main.py:24` `discover_branches(root_url)` → `discovery.py:8-9` one
`AsyncWebCrawler().arun(root_url)` call → `discovery.py:11-39` links bucketed
into a `dict[category, set[url]]` → `main.py:30-39` rendered as a `rich.Table`.

This is a **single-page** scan. "Branches" are just the outbound links visible
on the root page's rendered HTML, grouped by first path segment or external
domain — there is no deeper site-structure discovery before the table is
shown.

**Branch selection → seed URLs + scope filter**
`main.py:41-61` — user picks IDs or `all`. For `all`, every discovered URL
across every category becomes a seed and `allowed_branch_prefixes = ["all"]`
(scope check disabled). For specific IDs, `allowed_branch_prefixes` is set to
**the literal list of leaf URLs found on the root page for that category** —
not a path prefix like `https://site.com/docs/`. See Gap #1 below; this is a
likely bug, not a design choice.

**Crawl → markdown → dispatch**
`main.py:107` `start_workers(selected_urls, allowed_branch_prefixes,
output_manager, max_concurrent)` → `orchestrator.py:59-82` seeds an
`asyncio.Queue`, opens one shared `AsyncWebCrawler`, spawns N `worker()`
tasks, `await queue.join()`, then cancels workers.

Each `worker()` (`orchestrator.py:7-57`): dequeue URL → skip if visited →
`crawler.arun(url)` → `result.markdown` → `await
output_manager.process_page(url, markdown)` → extract links from the same
result, resolve relative URLs, strip fragments, filter through the (buggy)
scope check, enqueue unseen links.

**Q&A extraction → JSONL**
`output_manager.py:99-140` `process_page()`: if `unified_jsonl` or
`split_jsonl` requested → `_generate_qa(markdown)`
(`output_manager.py:69-97`) — sends **first 4000 chars only** of the page's
markdown to the LLM (`asyncio.to_thread(self.llm.invoke, ...)`), strips a
` ```json ` fence by slicing, `json.loads`s the result, keeps dict entries
with both `instruction` and `response` keys. In-run dedup against
`self.seen_instructions` (lowercased exact match, preloaded from every
existing `data/*.jsonl` at startup via `_preload_existing_instructions()`,
`output_manager.py:54-67`). Unique pairs written via `aiofiles` under
`self.lock` to `data/unified.jsonl` and/or `data/{first-path-segment}.jsonl`.

**Chunk → embed → persist**
`output_manager.py:136-137,147-176` — if `build_rag`, runs
`_add_to_rag(url, markdown)` in a thread. `RecursiveCharacterTextSplitter
(chunk_size=2000)` splits the full page markdown into parent chunks (default
`chunk_overlap` — **not set explicitly, so it's the langchain default,
currently 200**), each parent is re-split by a 400-char child splitter (same
default overlap), each child becomes a `Document(page_content=child,
metadata={"source": url, "parent_text": parent_text})`, and
`self.chroma_client.add_documents(child_docs)` persists to `./chroma_db`
(collection `scraper_docs`).

**Divergence from the background description**: the description says
"custom Parent-Child chunking ... 2000-char parent / 400-char child" as if
overlap were considered — it isn't; both splitters run with the library
default overlap (200 chars on both the 2000- and 400-char splitters), so
child chunks overlap each other by ~50%. Confirmed empirically: 30 distinct
source pages produced 9,204 stored child embeddings (~307 chunks/page), and
87 (source, chunk-text) pairs already have exact duplicate chunk text within
a single page — before any re-run. See ROADMAP.

## Where description and code diverge (summary)

- **"Ollama cloud/local" choice** — doesn't exist. `main.py` offers exactly
  3 providers (NVIDIA NIM / Ollama / OpenAI), and Ollama's `base_url` is
  hardcoded to `http://localhost:11434/v1` in `config.py:29-32`. No env var
  selects cloud vs local. `llm_factory.py:39` even has a comment claiming
  the chat model "Routes to OLLAMA CLOUD" — it doesn't; it's local-only as
  written.
- **Branch-scoped crawling** — likely broken past the seed page (see Gap #1).
- **"Zero-warning, fully functional, tested"** — no test files exist anywhere
  in the repo, so "tested" isn't verifiable and there's nothing to run.
- **Retrieval/RAG "database"** — write-only. No query/retrieval code exists
  anywhere in the repo (`grep` for `similarity_search`, `as_retriever`,
  `.query(` returns nothing). Chroma is populated but nothing ever reads
  from it.
- **Q&A quality fix ("ignore nav/menus/footers")** — the system prompt says
  this, but the actual data on disk (`data/en.jsonl`, `data/unified.jsonl`)
  is almost entirely questions *about* the navigation menu structure ("What
  main sections are listed in the navigation menu...", "What installation
  methods are documented..."), not technical/API content. Either the prompt
  fix doesn't work in practice, or the crawled pages were mostly
  boilerplate-heavy and the LLM had nothing else to extract from. Worth
  re-testing against a code-heavy page, not just doc-index pages.
