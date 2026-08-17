# Roadmap

Findings from the 2026-08-18 repo audit, ranked by severity. Nothing here has
been implemented — one-line proposed fix + rough size estimate per item.
Sizes: XS (<30 min), S (<2h), M (half day), L (multi-day).

## (a) Broken / blocking

1. **Branch-scoped crawling is almost certainly broken past the seed page.**
   `main.py:57-58` sets `allowed_branch_prefixes` to the literal list of leaf
   URLs found *on the root page* for the chosen category, not a path prefix.
   `orchestrator.py:41-48`'s scope check does `href.startswith(branch)`
   against those exact URLs, so almost no newly-discovered link during the
   crawl will match. In practice, choosing anything other than "all" likely
   crawls only the handful of URLs visible on page one.
   *Fix*: derive the prefix from the category itself (e.g.
   `f"{urlparse(root_url).scheme}://{domain}/{first_segment}/"`) instead of
   the discovered URL list. **Size: S.**

2. **Dependency pins are not reproducible from any tracked file.**
   `pyproject.toml` has no `[project]`/dependency section (only `[tool.ruff]`),
   `requirements.txt` has no version pins at all, and `uv.lock` is 3 lines —
   no packages resolved. The `numpy<2` / working `langchain`/`chromadb`/
   `crawl4ai` combination that's actually installed in `.venv` only exists
   because someone `pip install`'d it by hand into that venv. Delete `.venv`
   today and rebuild from what's tracked, and you get NumPy 2.x again — the
   "already solved" NumPy problem returns.
   *Fix*: `uv add` the real dependency set (crawl4ai, langchain,
   langchain-chroma, langchain-openai, langchain-text-splitters,
   python-dotenv, rich, aiofiles, pydantic, requests, plus explicit
   `numpy<2` and `urllib3<2.0.0` pins) into `pyproject.toml`, then `uv lock`.
   Ask before running — installs/upgrades need sign-off per session rules.
   **Size: S.**

3. **Chroma is write-only — no retrieval/query code exists anywhere in the
   repo** (`grep` for `similarity_search`, `as_retriever`, `.query(` returns
   nothing across all `.py` files). The "Vector RAG database" output is
   currently a database nobody can read from.
   *Fix*: a small `query.py` / CLI flag wrapping
   `chroma_client.similarity_search(query, k=...)`, returning `parent_text`
   from metadata rather than the child chunk. **Size: S–M.**

## (b) Fragile — will bite us soon

4. **Secrets sitting in a plaintext `.env`** — a commented-out but intact
   NVIDIA API key, and an `ollama=...` line holding what looks like a token
   that isn't even read by any code (`config.py` never calls
   `os.getenv("OLLAMA...")`; Ollama's key is hardcoded to the literal string
   `"ollama"`). No git repo exists yet, so nothing has leaked via history —
   but there's also no `.gitignore`, so the first `git init` + commit would
   bake these into history permanently.
   *Fix*: add `.gitignore` (must include `.env`) before any `git init`;
   replace `.env` with a committed `.env.example` and keep real secrets
   local-only; rotate the NVIDIA key since it's already sat in cleartext.
   **Size: S.**

5. **Child chunks overlap ~50%, inflating embedding cost and hurting
   retrieval precision.** Both `RecursiveCharacterTextSplitter`s in
   `output_manager.py:41-42` use the library's default `chunk_overlap`
   (200), which on a 400-char child splitter means every child chunk shares
   half its content with its neighbor. Measured on the current `chroma_db`:
   30 distinct source pages → 9,204 stored embeddings (~307 chunks/page),
   with 87 (source, chunk-text) pairs already exact-duplicated within a
   single page before any re-run.
   *Fix*: set `chunk_overlap` explicitly and much smaller (e.g. 0–50) on the
   child splitter. **Size: S.**

6. **No upsert / stable IDs into Chroma — re-running over the same site
   re-embeds and re-inserts everything as new vectors.** `add_documents` is
   called with no `ids=` anywhere in `_add_to_rag` (`output_manager.py:173`),
   so Chroma has no way to recognize "I've already indexed this chunk."
   JSONL is protected from row duplication by the instruction-text dedup,
   but Chroma has no equivalent guard.
   *Fix*: derive a deterministic id (e.g. `sha256(url + child_text)`) and
   pass `ids=` to `add_documents`, which makes it an upsert. **Size: S–M.**

7. **No resumability.** `visited` (`orchestrator.py:61`) is a plain in-memory
   `set()`. If the process dies at page 400 of 500, restarting re-crawls and
   re-pays for LLM extraction on all 400 already-done pages (JSONL text-dedup
   prevents duplicate *rows*, but doesn't save the re-fetch/re-inference
   cost), and re-inserts duplicate Chroma vectors per #6.
   *Fix*: persist visited URLs to a small sqlite file or newline-delimited
   log, reload on startup. **Size: M.**

8. **Embedding-model identity isn't recorded anywhere.** `embed
   ding_model_name` defaults to `nomic-embed-text` (`llm_factory.py:34`) but
   nothing stores which model produced a given collection's vectors. If a
   future run indexes with a different embedding model into the same
   `scraper_docs` collection, similarity search silently mixes incompatible
   vector spaces with no error.
   *Fix*: write the embedding model name into Chroma collection metadata (or
   a `meta.json` beside `chroma_db/`) at creation time, and refuse/warn if a
   later run's model doesn't match. **Size: S.**

9. **No rate limiting, politeness delay, or robots.txt handling** in
   `discovery.py` or `orchestrator.py`. N concurrent workers (default 5,
   user-configurable, no upper bound) hit the target site as fast as
   crawl4ai can fetch, with no backoff.
   *Fix*: add a per-domain delay between requests and a robots.txt check
   before enqueueing a URL. **Size: S–M.**

10. **Unbounded / off-target crawl scope when "all" is selected.**
    `allowed_branch_prefixes = ["all"]` fully disables the scope check in
    `orchestrator.py:41`, and `discovery.py` already buckets external-domain
    links as selectable categories. If the user picks "all", the crawl can
    walk onto arbitrary external domains discovered via links on the seed
    page — bounded only by the `visited` set, which in practice bounds
    nothing. This is a cost risk (LLM calls on off-target pages) and a
    soft SSRF-adjacent risk (the crawler will fetch whatever URL a link on
    the seed page points to, including internal/local addresses if the
    target site is compromised or user-controlled).
    *Fix*: default to same-domain-only even when "all" is chosen; require
    an explicit per-category opt-in to include external domains (the UI
    already supports selecting external categories individually — just stop
    "all" from silently including them). **Size: S.**

11. **LLM JSON output isn't schema-enforced.** `_generate_qa`
    (`output_manager.py:69-97`) relies on prompt instructions + manual
    ` ```json ` fence stripping + `json.loads`. Any deviation (extra prose,
    truncated output, unbalanced fences) throws inside a bare
    `except Exception`, gets printed, and the page silently contributes zero
    Q&A pairs with no retry.
    *Fix*: use provider-native JSON mode
    (`response_format={"type": "json_object"}`) where the provider supports
    it, keep the try/except as a fallback for providers that don't. **Size: S.**

12. **Shutdown swallows a spurious `task_done()` call.**
    `orchestrator.py:52-57` — on cancellation while a worker is blocked
    inside `await queue.get()` (the normal state after `queue.join()`
    returns and all items are drained), `CancelledError` fires before an
    item is dequeued, but the `finally: queue.task_done()` still runs
    unconditionally, raising `ValueError: task_done() called too many
    times`. Currently masked by `asyncio.gather(..., return_exceptions=True)`
    in `start_workers`, so it's silent today — but it could also mask a
    *different* real exception thrown during shutdown by looking identical
    in logs.
    *Fix*: only call `task_done()` in the branch that actually got an item
    (move it out of `finally`, call it explicitly after processing).
    **Size: XS.**

## (c) Missing capability

13. **No token/cost accounting** for paid providers (OpenAI, NVIDIA NIM) — no
    usage capture, no running total, no visibility into spend until the bill
    arrives.
    *Fix*: read `response.usage_metadata` (exposed by langchain's chat
    models) per call, accumulate, print a total at the end of the run.
    **Size: S.**

14. **No config surface** — chunk sizes (2000/400), default embedding model
    name, etc. are all hardcoded (`output_manager.py:41-42`,
    `llm_factory.py:33-34`).
    *Fix*: move these into `config.py` with env-var overrides and sane
    defaults. **Size: S.**

15. **No structured logging** — everything is `print()`. A long crawl's
    history disappears once the terminal scrolls past it; no log levels, no
    file output.
    *Fix*: swap `print` calls for stdlib `logging` with a file handler.
    **Size: S.**

16. **No tests anywhere in the repo.** The "fully functional, tested" claim
    isn't independently verifiable — there's nothing to run. The branch-scope
    bug (#1) is exactly the kind of thing a unit test on `discover_branches`
    + the orchestrator's scope filter (both pure logic, no network needed)
    would have caught.
    *Fix*: start with unit tests for `discover_branches`'s categorization,
    the orchestrator's `is_allowed` scope filter, and the JSONL dedup —
    none require network or LLM calls. **Size: M.**

## (d) Polish

17. **`duplicate.py` is stale and unwired.** Its own header comment
    (`# deduplicate.py`) doesn't match the filename, it's never imported or
    called by the pipeline, and it hardcodes exactly two filenames
    (`unified.jsonl`, `awesome.jsonl`) — `data/en.jsonl` exists on disk today
    and isn't covered.
    *Fix*: glob `data/*.jsonl` instead of hardcoding names; fix the stale
    comment. **Size: XS.**

18. **Dead import** — `from crawl4ai import cache_context` in
    `llm_factory.py:1`, unused anywhere in the file.
    *Fix*: delete the line. **Size: XS.**

19. **JSONL schema is a custom 2-key format** (`instruction`/`response`), not
    a named standard (not Alpaca's 3-key `instruction/input/output`, not
    ShareGPT, not OpenAI chat-fine-tuning format). Not wrong, but worth
    confirming it matches whatever fine-tuning pipeline will consume it.
    *Fix*: either document this as the intentional target schema, or add a
    converter step. **Size: XS–S.**

---

*Nothing in this list has been implemented. Highest-value first pass, if/when
we resume building: #1 (branch scope) and #2 (lockfile) are both small and
block trusting anything else the tool produces — the tool currently can't
reliably scope a crawl, and the environment that made everything else work
isn't reproducible.*
