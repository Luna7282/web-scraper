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

9. **[Partially resolved, step 5; Allow/Disallow precedence fixed step 8
   Part D] robots.txt is now respected, and correctly for Allow-overrides-
   Disallow; per-host rate limiting/politeness delay is still missing.**
   `robots_cache.py` + `crawl_worker`'s pre-fetch check (`pipeline.py`)
   now refuse a disallowed URL outright (`mark_permanently_failed`, no
   retry, logged loudly) — this applies uniformly, including to a branch
   the user explicitly selected; not configurable to override yet, which
   is a deliberate safety default worth revisiting only if a real need
   for an override shows up. `robots_cache.py` no longer delegates to
   `urllib.robotparser` — it resolved Allow/Disallow by file order
   instead of specificity, which broke real crawls against sites using a
   blanket `Disallow: /` plus a more specific `Allow:` for the one
   version they want crawled (`docs.manim.community`'s real robots.txt
   is exactly this shape) — see `LESSONS_LEARNED.md` #27.
   What's still missing: crawl_worker has no per-host concurrency
   semaphore or inter-request delay at all — N crawl workers still hit a
   single host as fast as they can claim work, with nothing throttling
   concurrent requests to the *same* host specifically (the original gap
   this item described). Confirmed against real data from the 5 fixture
   sites (`tests/fixtures/robots/`): none of the 5 specify `Crawl-delay`,
   so this hasn't bitten anything yet, and `_parse_rules()` doesn't even
   collect it (only allow/disallow directives) — if a real target site
   specifies one, it's silently dropped right now. *Fix*: add a per-host
   `asyncio.Semaphore` (concurrency cap) and honor `Crawl-delay` as a
   minimum inter-request spacing per host in `crawl_worker` — needs
   `_parse_rules()` extended to also collect a `Crawl-delay` value per
   group, since the stdlib parser this used to lean on for that is gone.
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

20. **[RESOLVED, step 8 Part A] Q&A extraction on a real page with zero
    markdown headings is untested.** Ran the real extraction path against
    `blog.cloudflare.com`'s root listing page (confirmed 0 markdown
    headings after stripping) -- 45 pairs, 7 factual claims spot-checked
    directly against source text, all accurate. See
    `LESSONS_LEARNED.md` #26.

21. **[RESOLVED, step 8 Part A] 4000-char truncation can land the
    extraction window on low-value content when a page front-loads
    boilerplate before its real content.** `extraction_units.py` +
    `config.EXTRACTION_STRATEGY` (default `PER_CHUNK`) replace the single
    truncated call with one call per parent chunk -- confirmed on the
    same FastAPI page: 84% of the resulting pairs (68/81) cover content
    the old 4000-char window could never reach. See
    `LESSONS_LEARNED.md` #26. **This fixed reachability only** -- see
    #22 and #23 below for what it didn't fix.

22. **`per_chunk` extraction doesn't reduce low-value pairs, only
    increases coverage.** Confirmed on the same FastAPI re-run
    (`LESSONS_LEARNED.md` #26): the sponsor/testimonial "low-value"
    pairs #25 flagged didn't disappear under `per_chunk`, they
    multiplied (13 pairs across 5 chunks). Every chunk gets extracted
    regardless of relevance -- a page that front-loads several low-value
    chunks before its real content produces more low-value pairs, not
    fewer.
    *Fix*: `TOP_K_CHUNKS_BY_RELEVANCE` is already implemented in
    `extraction_units.py` for exactly this case, but needs a real intent
    to rank chunks against (falls back to `per_chunk` without one) --
    hasn't been measured on a real page with a real intent yet. Try it
    against a page with a real intent set and compare the low-value-pair
    rate against this step's `per_chunk` baseline. **Size: S (mechanism
    exists; needs a measurement, not new code).**

23. **[Lower priority than #31 -- see there] No dedup for near-duplicate
    pairs from chunk-overlap boundaries or same-chunk paraphrase
    padding.** Measured, not assumed (`LESSONS_LEARNED.md` #26): ~27%
    of FastAPI's `per_chunk` pairs and ~47% of Cloudflare's are
    near-duplicates by answer-text similarity, from two distinct causes
    -- adjacent chunks re-covering the ~10% overlap region, and the "3
    to 5 diverse pairs" prompt instruction padding with rephrasings when
    a chunk only supports one real fact. `export_formats.py::dedup_by_question`'s
    exact-normalized-text dedup catches neither, since every pair is a
    fresh generation with different wording. Re-measured on real
    Part D data in step 8 Phase 2B/2C (`LESSONS_LEARNED.md` #33): 380
    same-chunk near-duplicate pairs (52.7% of chunks affected) vs. only
    24 pairs (2.1%) directly attributable to overlap -- same-chunk
    padding is the far bigger of the two causes on real data,
    confirming semantic dedup (catches both) over a chunking-overlap
    change (catches only the smaller one).
    *Fix, not yet chosen*: options include semantic similarity dedup at
    export time (embed each question, cluster/threshold, keep one per
    cluster -- costs embed calls per row), a smaller `top_k`-per-page
    instruction to the LLM to reduce same-chunk padding directly, or
    reducing `PARENT_CHUNK_OVERLAP` to shrink the boundary-duplicate
    contribution specifically (only one of the two causes, per the
    measurement). Needs a decision informed by cost, not made here.
    **Size: M.**

24. **Whether a listing/index page should pass `extract_threshold` at
    all is unanswered.** `blog.cloudflare.com`'s root (item 20 above) is
    grounded and accurate but mostly produces "who wrote/when was X
    published" trivia about *other* articles, since that's genuinely
    most of what a listing page's markup contains -- not an extraction
    defect, but a relevance-gating question: is this page worth
    extracting from at all, or should relevance scoring (step 6) learn
    to recognize and skip listing/index pages specifically.
    *Fix, not yet chosen*: no clear mechanism proposed yet -- would need
    either a structural signal (many short same-shaped teaser blocks) or
    a content-based one (relevance score against intent, which already
    exists but wasn't the mechanism that flagged this case). **Size:
    unclear until a detection approach is picked.**

25. **LLaMA-Factory/Axolotl packaging only has verified field mappings
    for 2-3 schemas each.** `export.py`'s `package_llama_factory` (alpaca,
    conversational, openai_finetune) and `package_axolotl` (alpaca,
    conversational, openai_finetune) refuse other schemas rather than
    emit an unverified `columns`/`type` mapping -- deliberate (see the
    comments at `_LLAMA_FACTORY_KNOWN_SCHEMAS`/`_AXOLOTL_TYPE_BY_SCHEMA`
    in `export.py`), not an oversight, but it does mean
    `prompt_completion`/`embedding_pairs`/`rag_eval`/`vertex` can only be
    exported as `plain-jsonl` or `mlx` today.
    *Fix*: check each framework's docs for the exact expected shape
    before adding it (Axolotl's `input_output` or `completion` types are
    plausible fits for `prompt_completion` but weren't confirmed this
    session). **Size: XS per schema, once confirmed.**

26. **[RESOLVED, step 8 Part D] `chrome_strip.py` was never called from
    `main.py`'s real fetch path.** Confirmed by grep across the whole
    codebase, not inference -- `strip_chrome`/`clean_html`/
    `strip_text_patterns` were called nowhere outside the module itself
    and its own test file, despite being built and fixture-validated
    since step 4. `_make_fetch_fn` now passes chrome_strip's excluded-
    tags/selector into the real `CrawlerRunConfig` and applies
    `strip_text_patterns` to the result; a real integration test
    (`tests/test_fetch_fn_integration.py`, via crawl4ai's `raw:` URL
    scheme) exercises this through `main.py` itself, not `strip_chrome()`
    in isolation. See `LESSONS_LEARNED.md` #28.

27. **No per-stage integration test existed for any real pipeline stage
    before step 8 Part D.** The chrome-stripping gap (#26) and the
    robots.txt Allow/Disallow precedence bug (`LESSONS_LEARNED.md` #27)
    were both caught only by a real end-to-end crawl, not by the
    existing test suite -- every prior test exercised a function
    correctly but never confirmed `main.py` actually calls it. One
    integration test now exists for the fetch stage
    (`test_fetch_fn_integration.py`); the same gap-shape could exist for
    other stages not yet covered this way.
    *Fix*: audit each pipeline stage main.py wires up (crawl, extract,
    write, RAG upsert, robots check) for a test that goes in through
    `main.py`'s own constructors (`_make_fetch_fn`, `_make_extract_fn`,
    etc.), not just the stage's underlying function in isolation. The
    step 8 Part D audit (`LESSONS_LEARNED.md` #28) confirmed every
    *other* stage's underlying function is at least correctly wired
    today -- this item is about adding the regression guard, not about
    a currently-known second instance of the bug. **Size: M.**

28. **`max_pages` can overshoot within a single uninterrupted run, not
    just across a resume.** `Frontier.claim()`'s cap check
    (`done+skipped_extract < max_pages`) only gates *new* claims -- it
    can't retroactively cancel rows already claimed and mid-flight.
    Confirmed on both step 8 Part D runs: with 3 crawl workers racing
    ahead of 2 slow (per_chunk, multi-call) extract workers, many rows
    sat `in_progress` simultaneously while `done` was still under the
    cap; all of them finished and incremented `done` regardless, so
    `max_pages=20` produced 35 `done` pages both times, not a hard
    ceiling. Distinct from the already-documented "a resumed run keeps
    claiming past the cap" case (item 7's note, still real) -- this
    overshoot happens within one continuous run, no crash or resume
    needed, and scales with worker-count × per-page latency.
    *Fix, not yet chosen*: options include a soft "stop admitting new
    in-flight work" signal that also reduces effective worker
    concurrency as `done` approaches the cap, or just documenting
    `max_pages` as "budget, not ceiling" more prominently in the CLI
    prompt itself (it currently only says this in `CLAUDE.md`). Whoever
    sizes `max_pages` for cost control needs to know the real ceiling
    can be `max_pages + (concurrent in-flight rows at cap time)`, not
    `max_pages` itself. **Size: S if just documented more prominently in
    the CLI; M if an actual throttle is built.**

29. **[Partially resolved, step 8 Phase 2A] `derive_section` counted
    depth from the domain root, not the crawl's own seed prefix --
    every page in a real crawl got the same section.** Fixed:
    `content/sectioning.py::derive_section()` now accepts an optional
    `seed_prefixes` list and counts depth relative to the longest
    matching seed prefix, reusing `crawl/scope.py::derive_prefix`'s own
    output rather than reimplementing prefix matching. See
    `LESSONS_LEARNED.md` #30 for the measured before/after and the
    depth-1/2/3 comparison. **Still open**: whether `config.
    SECTION_DEPTH`'s default (2) should change -- depth=1 produced the
    expected clean categories on the one real site measured, depth=2/3
    degenerated to near-per-page granularity, but this is one site's
    URL structure, not a robust multi-site conclusion. *Fix*: measure
    against 2-3 more real sites with genuinely deeper category
    structure before changing the default. **Size: XS to change the
    constant, once measured.**

30. **`export/export.py`'s `--section-depth` CLI argument is a no-op.**
    Threaded through to `package_plain_jsonl(..., section_depth)` but
    never read inside that function -- sections are grouped purely by
    the `section` field already baked into the canonical record at
    crawl time. `--section-depth 1` and `--section-depth 3` against the
    same canonical file produce byte-identical output, confirmed by
    running both. Found while measuring `LESSONS_LEARNED.md` #30, not
    previously known.
    *Fix, not yet chosen*: either remove the flag entirely (depth is
    crawl-time-only, so an export-time flag claiming to control it is
    actively misleading) or make export.py actually re-derive `section`
    from `source_url` at the requested depth before grouping -- which
    would need the crawl's seed prefixes recorded somewhere export.py
    can read them (not currently captured anywhere outside the
    now-baked `section` field itself). The dataset card seems like the
    natural place to record seed prefixes if this fix is chosen. **Size:
    XS to remove the flag; S-M to make it real.**

31. **[Ranked above #23 -- larger measured impact] Markdown link syntax
    consumes ~44% of every chunk's character budget as pure URL/syntax
    overhead, not content.** Measured across all 264 real chunks in the
    Part D corpus (`LESSONS_LEARNED.md` #31): 46.7% of all chunk
    characters sit inside `[text](url)` link syntax; only 2.9% of that
    is the visible text a reader would see, so **43.8 percentage points
    is pure overhead** -- consistent per-chunk (mean 45.6%, median
    45.0%), not a few outliers, reaching 89.5% on the worst chunk (a
    font-template table where every row is 8 characters of real symbol
    name wrapped in ~230 characters of repeated URL and a duplicated
    title attribute). This taxes everything that touches chunk text:
    `MAX_EXTRACT_CHARS`/`MAX_EMBED_CHARS` truncation windows effectively
    cover about half the real content their character counts suggest on
    a reference-heavy site, and it's a real, previously-unquantified
    contributor to the original pre-rebuild audit's 9,204-vectors-from-
    30-pages figure -- more chunks were needed partly because each one
    carried less actual content than its length implied. Ranked above
    #23's near-duplicate work specifically because the measured impact
    is larger and touches every chunk, not a redundancy subset of pairs.
    *Fix, not yet chosen -- evaluate, don't guess, when this is picked
    up*: crawl4ai's `DefaultMarkdownGenerator` may have options that
    already address this (not checked); a post-conversion transform
    reducing `[text](url "title")` to just its visible text, with URLs
    either dropped entirely or collected into a per-chunk reference
    list, is the other candidate shape. **The tradeoff to settle before
    picking one**: on API reference pages specifically, the link target
    often *is* information -- a method name links to its own canonical
    URL, and stripping that wholesale could lose real signal on exactly
    the pages that are densest with links, not just remove noise. Needs
    real measurement of what's lost, not an assumption that all link
    overhead is equally disposable. **Size: S-M once a direction is
    chosen; the direction itself needs the tradeoff measured first.**

---

*Nothing in this list has been implemented. Highest-value first pass, if/when
we resume building: #1 (branch scope) and #2 (lockfile) are both small and
block trusting anything else the tool produces — the tool currently can't
reliably scope a crawl, and the environment that made everything else work
isn't reproducible.*
