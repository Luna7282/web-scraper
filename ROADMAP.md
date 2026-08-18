# Roadmap

Findings from the 2026-08-18 repo audit, ranked by severity. Nothing here has
been implemented — one-line proposed fix + rough size estimate per item.
Sizes: XS (<30 min), S (<2h), M (half day), L (multi-day).

## (a) Broken / blocking

1. **[RESOLVED, step 6] Branch-scoped crawling was almost certainly broken
   past the seed page.** This described the *old* `orchestrator.py` path
   (`main.py:57-58`'s `allowed_branch_prefixes` set to literal leaf URLs,
   `orchestrator.py:41-48`'s naive `startswith` check). Step 3 fixed the
   scope predicate itself (`scope.py`'s `derive_prefix`/`is_in_scope`,
   tested against 5 real sites) but only wired it into `--dry-run` at the
   time — the *live* crawl path (`orchestrator.py`) still had the original
   bug. Step 6's `main.py` rewrite finally applies the corrected predicate
   to the real crawl (`_make_scope_check` in `main.py`, feeding
   `crawl_worker`), so this is now fixed in the path that actually runs,
   not just in the diagnostic tool. `orchestrator.py` itself still has the
   original bug — left as-is, unreferenced, pending step 9 deletion.

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

3. **[RESOLVED, step 7] Chroma is write-only — no retrieval/query code
   exists anywhere in the repo.** `query.py::query_chunks()`/
   `format_query_results()` embed a question, search child chunks, and
   return parent text + sources + the matched child + distance;
   `main.py --query "<question>"` wires it up. Proven against a real
   index (3 fixtures, real local-Ollama embeddings) with 3 real queries,
   not just code review — see `LESSONS_LEARNED.md` #23.

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

5. **[Partially resolved, step 7] Child chunks overlap ~50%, inflating
   embedding cost and hurting retrieval precision.** The overlap is now
   an explicit, visible `config.py` value (`CHILD_CHUNK_OVERLAP=200` on
   `CHILD_CHUNK_SIZE=400`) instead of an invisible library default — see
   `LESSONS_LEARNED.md` #22. **Still 200/50%, unchanged** — making it
   visible and shrinking it were deliberately kept as separate decisions;
   this item stays open until the value itself is revisited.
   *Fix*: lower `CHILD_CHUNK_OVERLAP` in `config.py` (e.g. 0–50) once
   there's a reason to spend the (now-visible) cost/precision tradeoff.
   **Size: S.**

6. **[RESOLVED, step 7] No upsert / stable IDs into Chroma — re-running
   over the same site re-embeds and re-inserts everything as new
   vectors.** `chunk_store.py::chunk_id()`/`ChunkStore.add_or_merge_chunk()`
   implement exactly the revised fix below — hash the normalized chunk
   text alone, `sources` as a list, merge (not overwrite) on collision.
   Verified against real archived duplicate data
   (`tests/test_chunk_store_archived_data.py`) and against a real live
   index of two freshly-fetched pages known to share content (46 of 146
   chunks collided) — see `LESSONS_LEARNED.md` #19/#23. The provenance
   tradeoff noted below is honored: `sources` is surfaced in every
   `QueryResult` returned by `query.py`, not stored and ignored.
   *Original fix note, revised (step 4)*: originally planned as `sha256(url + child_text)`
   — wrong once cross-page content duplication is accounted for (step 4's
   chunk-dump analysis found real content genuinely repeated verbatim
   across different pages, e.g. identical install instructions on every
   OS's page — category C in `LESSONS_LEARNED.md` #4's amendment).
   Hashing `url + text` gives each page's copy a different ID, so those
   duplicates would survive upsert entirely and still produce N
   near-identical competing vectors in retrieval. Hash the **normalized
   chunk text alone** — `sha256(child_text)` — so the same content from
   any page maps to the same Chroma id. Carry `sources` as a **list** in
   metadata instead of a single `source` string: first write creates the
   record, a subsequent identical chunk from a different page appends its
   URL to the existing list rather than creating a second vector.
   **Tradeoff, stated explicitly**: one canonical chunk means a retrieval
   hit returns one attribution path, not "this exact sentence also
   appears on 4 other pages" — the `sources` list is what preserves that
   provenance instead, so it must actually get surfaced wherever query
   results are used, not just stored and ignored. **Size: S–M.**

7. **No resumability.** `visited` (`orchestrator.py:61`) is a plain in-memory
   `set()`. If the process dies at page 400 of 500, restarting re-crawls and
   re-pays for LLM extraction on all 400 already-done pages (JSONL text-dedup
   prevents duplicate *rows*, but doesn't save the re-fetch/re-inference
   cost), and re-inserts duplicate Chroma vectors per #6.
   *Fix*: persist visited URLs to a small sqlite file or newline-delimited
   log, reload on startup. **Size: M.**

8. **[RESOLVED, step 7] Embedding-model identity isn't recorded
   anywhere.** `chunk_store.py::get_or_create_collection()` writes
   `embedding_model`/`embedding_dim` into the Chroma collection's own
   metadata at creation; `verify_embedding_identity()` is called on both
   the write path and `query.py::query_chunks()`, raising
   `EmbeddingIdentityMismatch` (loud exception, not a warning) on a
   mismatch. Includes dimensionality alongside the model name as asked
   — see `LESSONS_LEARNED.md` #21.

9. **[Partially resolved, step 5] robots.txt is now respected; per-host
   rate limiting/politeness delay is still missing.** `robots_cache.py` +
   `crawl_worker`'s pre-fetch check (`pipeline.py`) now refuse a
   disallowed URL outright (`mark_permanently_failed`, no retry, logged
   loudly) — this applies uniformly, including to a branch the user
   explicitly selected; not configurable to override yet, which is a
   deliberate safety default worth revisiting only if a real need for an
   override shows up. What's still missing: crawl_worker has no per-host
   concurrency semaphore or inter-request delay at all — N crawl workers
   still hit a single host as fast as they can claim work, with nothing
   throttling concurrent requests to the *same* host specifically (the
   original gap this item described). Confirmed against real data from
   the 5 fixture sites (`tests/fixtures/robots/`): none of the 5 specify
   `Crawl-delay`, so this hasn't bitten anything yet, but `RobotFileParser`
   parses it fine (`parser.crawl_delay(ua)`) and `HostPolicy` never reads
   it — if a real target site does specify one, it's silently ignored
   right now. *Fix*: add a per-host `asyncio.Semaphore` (concurrency cap)
   and honor `Crawl-delay` as a minimum inter-request spacing per host in
   `crawl_worker`, sourced from `HostPolicy.robots_parser.crawl_delay()`.
   **Size: S–M.**

9a. **Sitemap discovery doesn't follow a `sitemapindex`.** Confirmed
    against real data: `blog.cloudflare.com/sitemap.xml` is a
    `<sitemapindex>` (points at further sitemaps, not URLs directly) —
    a common pattern on large sites, not a one-off. `robots_cache.py`
    records the sitemap URL but never fetches or parses its content, so
    this doesn't currently affect anything, but if sitemap-based seeding
    is added later it needs to recurse one level (parse `<sitemapindex>`,
    fetch each child `<sitemap>`, only then expect a `<urlset>`) rather
    than assuming every `/sitemap.xml` is directly a flat URL list.
    *Fix*: when sitemap content is actually consumed, check the root tag
    first (`urlset` vs `sitemapindex`) and recurse one level for the
    latter. **Size: S.**

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

10a. **`frontier.py`'s claim query is host-blind.** `_locked_claim` picks
    the oldest `queued` row regardless of host. Fine for a single-host
    crawl (the only case exercised so far), but once cross-host crawling
    with per-host politeness semaphores lands, a worker can claim a URL for
    a host that's currently saturated (semaphore full) and block holding
    that `in_progress` row while other hosts' `queued` rows sit unclaimed —
    other workers can't help since claim() doesn't know to skip a
    host-blocked row and try a different host's. Not fixed now; the
    single-host case this project currently targets doesn't hit it.
    *Fix*: claim query needs a host-aware `WHERE` clause (skip hosts
    currently at their concurrency limit) once multi-host crawling is
    actually exercised. **Size: M.**

11. **[Partially resolved, step 5] LLM JSON output isn't schema-enforced.**
    Described the *old* `_generate_qa` (`output_manager.py:69-97`): prompt
    instructions + manual fence-stripping + `json.loads`, any deviation
    throws inside a bare `except Exception`, silently zero pairs, no
    retry. The *new* pipeline's `extraction.py::parse_qa_json` improves on
    this — tries direct parse, fence-stripped, and prose-substring-salvage
    before raising `MalformedExtractionError`, which `extract_worker`
    retries (up to `MAX_RETRIES`) rather than silently swallowing. Still
    not provider-native JSON mode (`response_format={"type":
    "json_object"}`), which would prevent malformed output rather than
    salvage/retry around it — worth adding on top, not a substitute for
    what's there now. `output_manager.py` itself is unchanged (dead code
    path, not deleted). *Fix, remaining*: add
    `response_format={"type": "json_object"}` where the provider supports
    it, on top of the existing salvage/retry. **Size: S.**

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

14. **[Partially resolved, step 7] No config surface** — chunk sizes are
    now explicit in `config.py` (`PARENT_CHUNK_SIZE`/`_OVERLAP`,
    `CHILD_CHUNK_SIZE`/`_OVERLAP`, see `LESSONS_LEARNED.md` #22). Still
    open: no env-var override mechanism for any config value yet
    (these are plain module constants), and the embedding model
    name/dimension (`EMBEDDING_MODEL_NAME`/`EMBEDDING_DIM`) live as
    constants in `main.py`, not `config.py`.
    *Fix*: move `EMBEDDING_MODEL_NAME`/`EMBEDDING_DIM` into `config.py`
    alongside the chunk constants; add env-var overrides for all of them.
    **Size: S.**

15. **No structured logging** — everything is `print()`. A long crawl's
    history disappears once the terminal scrolls past it; no log levels, no
    file output.
    *Fix*: swap `print` calls for stdlib `logging` with a file handler.
    **Size: S.**

15a. **Pagination is in-scope by prefix but usually worthless to crawl.**
    `is_in_scope` (`scope.py`) only checks host + path prefix — query strings
    are normalized (sorted) but never examined for scope decisions. A URL
    like `.../blog/?page=2` is accepted whenever its base path is in-scope,
    same as any other query-param variant. Confirmed as a real, unaddressed
    case during step 3's multi-site testing, though none of the 5 test
    sites' root pages happened to expose live pagination links to verify
    against end-to-end. Deliberately out of scope for the scope predicate
    itself — this is a relevance/extraction-quality concern (a paginated
    listing page's *content* is rarely worth extracting, not that it's
    off-topic), closer to step 6's relevance gating than step 3's host/prefix
    scoping.
    *Fix*: a configurable exclude-pattern list (e.g. glob/regex against path
    or query, like `page=`, `/page/\d+/`) checked in `is_in_scope` or as a
    separate filter — generic config, not site-specific code, per the "no
    site-specific logic in scope.py" rule. **Size: S.**

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
