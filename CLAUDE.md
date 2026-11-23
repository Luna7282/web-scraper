# CLAUDE.md

Guidance for working in this repo. Keep this current — update it whenever
the setup, workflow, or architecture invariants below change, not just when
someone remembers to.

## What this is

A general-purpose, prompt-steered crawler for any website — give it a root
URL and a natural-language intent, and it crawls, judges pages against that
intent, and turns the result into training datasets (multiple schemas and
framework-specific packagings via `export/export.py`) plus a Chroma RAG
index. It
is **not** documentation-specific: docs sites are one input type this
project has been tested against, not the target. Don't let "docs"/
"documentation" framing creep back into how this tool is described — it's
a scope regression, not a simplification.

See `ARCHITECTURE.md` for the as-built system and `ROADMAP.md` for known
gaps. `LESSONS_LEARNED.md` is the running problem → root cause → fix log —
**append to it after every significant step or run**, not just when
something breaks.

Currently mid-rebuild per a step-by-step plan (steps tracked outside this
file, in-session). The pre-rebuild source is preserved in git history —
see the "Snapshot pre-rebuild source as-is" commit — before assuming
something described in `ARCHITECTURE.md` is still accurate, check whether a
later commit has already changed it.

## File map

Package-by-role (moved from a flat 24-file root — step 8 Phase 1B). All
imports are absolute (`from crawl.frontier import ...`), including
within a package — this codebase has no relative imports, by convention,
not by accident.

| Package | Module | Role |
|---|---|---|
| `crawl/` | `frontier.py` | SQLite-backed crawl state machine, quiescence, retries, cancellation |
| | `pipeline.py` | `crawl_worker`/`extract_worker`/`writer_worker`/`map_worker` — the real loops `main.py` runs |
| | `robots_cache.py` | robots.txt/sitemap.xml/llms.txt, own RFC 9309 Allow/Disallow resolver |
| | `sitemap.py` | `parse_sitemap_urls` — pure XML parsing, not yet wired into map mode (`ROADMAP.md` #42) |
| | `scope.py` | `normalize_url`/`derive_prefix`/`is_in_scope` — pure, no site-specific logic |
| | `discovery.py` | `discover_branches`/`fetch_page_links` — one-page link discovery |
| | `graph.py` | `discovery_graph`/`path_tree`: JSON graph projections over a frontier DB (`--graph`) |
| | `live_status.py` | `read_live_status`: poll-friendly crawl-state snapshot, own connection (`--live-status`) |
| | `score_report.py` | `--score-report`: relevance-score distribution from a prior run |
| | `circuit_breaker.py` | `LlmCircuitBreaker`: opt-in (`config.LLM_BREAKER_THRESHOLD`, off by default) gate that pauses LLM calls after N consecutive 429s, stops the run on a daily-quota signal |
| | `timing.py` | `TimingLog`: opt-in (`config.EXTRACT_TIMING_LOG`, off by default) JSONL timing of fetch/score/LLM calls → `<run_dir>/timings.jsonl` |
| `content/` | `chrome_strip.py` | Nav/header/footer/aside/button exclusion, applied in `main.py::_make_fetch_fn` |
| | `extraction.py` | `parse_qa_json`, the Q&A system prompt |
| | `extraction_units.py` | Extraction-strategy chunk selection (`first_n_chars`/`per_chunk`/`top_k_chunks_by_relevance`) |
| | `sectioning.py` | `derive_section` (canonical record field) + filename slugify/cap/disambiguate (export time) |
| | `relevance.py` | Embedding-based relevance scoring (`score_headings`, chosen by measurement) |
| | `balance.py` | Contract v2 source balancing: row caps, low-value page heuristic, `rebalance_rows` |
| `generators/` | `__init__.py` | Contract v2 `Generator` interface (`wants`/`request`/`parse`), registry, `parse_json_array` |
| | `qa.py` | `qa@1`: today's Q&A extraction as a generator (same prompt, request and parser) |
| | `code_example.py` | `code_example@1`: instruction/code pairs from units with fenced code |
| `verifiers/` | `__init__.py` | Contract v2 `Verifier` interface, registry, `VerifierRunner` (drop/flag, statuses, report) |
| | `schema.py` | `schema@1`: generator JSON Schema + export rules; code must appear in the exact text sent |
| | `near_duplicate.py` | `near_duplicate@1`: same/near-same question or instruction+code within a run (order-dependent) |
| | `code_syntax.py` | `code_syntax@1`: `ast.parse` / `json.loads` only, never executes |
| | `external.py` | `external@1`: a supplied command, no shell, own process group, minimal env, timeout, stdout cap |
| `storage/` | `writer.py` | Sole owner of the canonical JSONL file + Chroma upsert calls |
| | `chunk_store.py` | Content-hash chunk IDs, `ChunkStore.add_or_merge_chunk`, embedding-identity check |
| | `canonical.py` | The canonical record schema + `build_canonical_record` |
| | `query.py` | `--query`: embed a question, search Chroma, print parent text |
| `export/` | `export.py` | `-m export.export`: canonical.jsonl → training-format files (separate CLI) |
| | `export_formats.py` | Pure schema projections + validate/dedup/split (no I/O) |
| | `dataset_report.py` | `--dataset-report`: redundancy/duplicate diagnostics over a completed run |
| `server/` | `app.py` | Flask app + every route (`create_app()`) — localhost-only, no auth |
| | `jobs.py` | `JobManager`: subprocess-launched, PID-file-tracked jobs (map/crawl/export runs) |
| | `paths.py` | `run_dir_for()` — the one place a URL-supplied run_id is resolved to a filesystem path |
| | `runs.py` | `list_runs`/`summarize_run` — run-history listing for `GET /runs` |
| (root) | `server.py` | `uv run python server.py` — the control server's entry point |
| | `main.py` | Interactive crawl CLI + `--dry-run`/`--score-report`/`--query`/`--dataset-report`/map-cancel-select-graph-live-status flags/`--headless-config` (non-interactive, server-launched) |
| | `config.py` | All tunable constants — no config value hardcoded in the module that uses it |
| | `llm_factory.py` | `get_llm()`, `LocalOllamaEmbeddings` (the one hard provider-routing invariant, below) |
| | `progress_display.py` | Read-only Rich live view against the frontier |

## Environment / uv workflow

- Python 3.12, managed via `uv`. The working venv lives at `.venv/`
  (gitignored — never committed).
- `pyproject.toml` is the source of truth for dependencies; `uv.lock` pins
  exact versions. **Do not hand-edit `uv.lock`** — change
  `pyproject.toml` and run `uv lock`.
- `requirements.lock.txt` (repo root) is a one-time `uv pip freeze`
  snapshot of the venv taken before the dependency manifest was rebuilt —
  historical record only, not maintained going forward, not what `uv sync`
  reads.
- To reproduce the environment: `uv sync`. This reads `pyproject.toml` +
  `uv.lock`, not `requirements.txt` (which may be stale/deleted — check
  `pyproject.toml` first).
- Two version pins matter more than they look: `numpy<2` and
  `urllib3<2.0.0`. Removing either reintroduces problems already solved
  once — see `LESSONS_LEARNED.md` #1. Don't relax them without checking
  that entry first.
- **Before any `uv` command that can install or uninstall (`uv sync`,
  `uv pip sync`, `uv pip install`, etc.), run it with `--dry-run` first,
  read the resolved target environment path out of the output, and
  confirm it's the one actually intended before running for real.**
  `--python <path>` only picks which interpreter/version uv resolves
  against — it does **not** redirect where packages get installed.
  `--active` prefers `$VIRTUAL_ENV` and silently falls back to the
  project's own `.venv` when nothing is active. Neither flag is a safe way
  to point `uv sync` at an arbitrary venv; a `uv sync` meant for a scratch
  environment mutated this project's live `.venv` once already —
  see `LESSONS_LEARNED.md` #6. The only structurally safe way to verify a
  manifest against an isolated environment is a full repo copy (source +
  `pyproject.toml` + `uv.lock`, no `.venv`) in a directory with no project
  of its own to fall back to.

## Running the CLI

```
uv run python main.py                              # interactive crawl
uv run python main.py --dry-run <url>               # scope-check only, no crawl/LLM
uv run python main.py --score-report data/run/frontier.db  # relevance distribution from a prior run
```

**Output layout** (step 8 Phase 1D): `data/run/` holds one crawl's state
— `frontier.db` (+ its `-shm`/`-wal` WAL files), `canonical.jsonl`, and
the Chroma index (`chroma_db/`, `config.CHROMA_PERSIST_DIR`) — never a
deliverable, always overwritten/resumed in place by the next run against
the same directory. `data/export/<format>/` (a `--out` argument you
choose per export call, this is just the convention) holds the actual
training-format files `export/export.py` produces — a separate, disposable
directory per export, since re-running export never touches `data/run/`.

Interactive prompts: root URL, **intent** (optional — blank means no
relevance filtering, every page extracts), branch/scope selection, LLM
provider + model (chat/extraction only — embeddings always local Ollama,
see the invariant below), max pages/depth, relevance thresholds
(**default 0 for both — log-only**, shown with an explicit on-screen
warning, not a buried default), crawl/extract worker concurrency.

Output is JSONL by default; a `build_rag` prompt (step 7) optionally wires
up a real Chroma collection (`storage/chunk_store.py`, persisted to
`config.CHROMA_PERSIST_DIR`) alongside it — chunk/embed/upsert happens in
`extract_worker`/`Writer` via injected `chunk_fn`/`chroma_upsert_fn`
closures built in `main.py`. There's no crawl-time per-section JSONL
split — `data/run/canonical.jsonl` is always one unified file; per-section
splitting happens at export time instead (`export/export.py`'s
`plain-jsonl` packaging, see below). Frontier state persists to
`data/run/frontier.db`; a rerun resumes it (`Frontier.recover_crashed()` at
startup) rather than starting over.

**Chrome-stripping runs on every real fetch**: `main.py::_make_fetch_fn`
passes `content/chrome_strip.py`'s `DEFAULT_EXCLUDED_TAGS`/
`DEFAULT_EXCLUDED_SELECTOR` into the real `CrawlerRunConfig` (crawl4ai's
own HTML-cleaning stage) and applies `strip_text_patterns()` to the
resulting markdown before it ever reaches extraction or Chroma chunking.
**This was not true until step 8 Part D** — see `LESSONS_LEARNED.md` #28
before assuming a component that's tested in isolation is therefore
wired into `main.py` — verify the call site, not just the function.

**Retrieval**: `uv run python main.py --query "<question>"` embeds the
question, searches child chunks in the existing Chroma collection, and
prints parent text + sources + the matched child chunk + distance
(`storage/query.py::query_chunks`/`format_query_results`). This is the
only code that reads from Chroma — see `ROADMAP.md` #3.

**Chunking**: parent/child sizes and overlap are explicit in `config.py`
(`PARENT_CHUNK_SIZE`/`_OVERLAP`, `CHILD_CHUNK_SIZE`/`_OVERLAP`), not a
library default — see `LESSONS_LEARNED.md` #22 before changing them
(200-char overlap on 400-char children is 50%, the documented cause of
9,204 vectors from 30 pages in the pre-rebuild audit; making that visible
and changing it are separate decisions).

**Chunk IDs are content hashes, not URL-scoped**: `storage/chunk_store.py::chunk_id()`
hashes normalized chunk text alone (whitespace-collapsed, lowercased,
trailing-punctuation-stripped — see `normalize_chunk_text()`), so the same
text repeated across pages collides to one Chroma id and its `sources`
metadata grows as a list instead of duplicating the vector — verified
against real cross-page duplicates, see `LESSONS_LEARNED.md` #19/#23.
**Use `ChunkStore.add_or_merge_chunk()` for every chunk write, never
`collection.add()`/`upsert()` directly** — `add()` silently no-ops on a
duplicate id (drops the new source), and `upsert()` replaces metadata
wholesale instead of merging the `sources` list; only
`add_or_merge_chunk()`'s get-then-update path preserves prior sources
(`LESSONS_LEARNED.md` #20).

**Embedding-model identity is checked loudly, not warned about**: every
Chroma collection records `embedding_model`/`embedding_dim` in its
metadata at creation (`get_or_create_collection()`); both the write path
and `query_chunks()` call `verify_embedding_identity()`, which raises
`EmbeddingIdentityMismatch` (not a log line) if the model in use doesn't
match what the collection was built with.

**Extraction is per-chunk by default, not a single truncated call**:
`config.EXTRACTION_STRATEGY` (default `PER_CHUNK`) drives
`content/extraction_units.py::select_extraction_units()`, which `extract_worker`
loops over — one LLM call per parent-sized chunk instead of one call over
`content[:MAX_EXTRACT_CHARS]`. `FIRST_N_CHARS` (the old behavior) and
`TOP_K_CHUNKS_BY_RELEVANCE` (embeds chunks, keeps only the most
intent-relevant — falls back to `PER_CHUNK` with no intent set) are also
available. **This fixes reachability only, not relevance or redundancy**
— see `LESSONS_LEARNED.md` #26 for the measured numbers and
`ROADMAP.md` #22/#23 for what's still open.

**The canonical record (`storage/canonical.py`) is the only file `Writer` ever
writes, and it is never a training file directly**: one row per Q&A pair
— question, answer, `source_chunk` (the exact unit that produced it —
non-negotiable, every downstream projection needs it), source_url,
section, page_title, generation_model, extraction_strategy, timestamp,
crawl_date, license_signal (best-effort text-pattern match, not a legal
determination).

**Export is a separate CLI, over the canonical file, never re-crawling**:
`uv run python -m export.export data/run/canonical.jsonl --schema alpaca
--framework huggingface --out data/export/...` — run as a module from the
repo root, not as a direct script (`python export/export.py` breaks: its
absolute `export.export_formats`-style imports need the repo root on
`sys.path`, which only `-m` guarantees). Pipeline, always in this order:
validate (`export/export_formats.py::validate_records` — rejects empty/
equal/too-short answers) → dedup (`dedup_by_question`, exact-normalized-
text only) → **optional** semantic dedup (`semantic_dedup`, pair-level
near-duplicate answers on the same page — the `ROADMAP.md` #23 gap
`dedup_by_question` leaves; off by default, enabled via `--semantic-dedup`,
threshold via `--semantic-dedup-threshold` default
`ANSWER_NEAR_DUP_THRESHOLD`=0.4; `--semantic-dedup-report` writes
`semantic_dedup_report.json` listing what the current threshold would
drop without dropping it, independent of whether `--semantic-dedup` is
on — the intended way to pick a threshold from real data rather than
guessing one, see `LESSONS_LEARNED.md` #37) → split (`split_records`,
grouped by section/source_url,
never random — docs repeat content across pages, a random split leaks
near-duplicates into eval; seeded for reproducibility) → project
(`SCHEMA_PROJECTIONS`/`BATCH_PROJECTIONS` — conversational, alpaca,
prompt_completion, raw_text, embedding_pairs, triplets, rag_eval,
openai_finetune, vertex; `UNSUPPORTED_WITHOUT_EXTRA_PASS` refuses
dpo/orpo/kto/classification loudly rather than faking them) → package
(mlx/huggingface/llama-factory/axolotl/plain-jsonl). Every export writes
a `dataset_card.json` (sites, crawl dates, intent, model, row counts,
license signals observed). **LLaMA-Factory/Axolotl only have verified
field mappings for a few schemas** (checked against their real docs, not
guessed — see `export/export.py`'s `_LLAMA_FACTORY_KNOWN_SCHEMAS`/
`_AXOLOTL_TYPE_BY_SCHEMA`); an unmapped schema+framework combination
refuses rather than emitting an unverified mapping.

**Section labels and export filenames are two different transforms**
(`content/sectioning.py`): `derive_section(url, depth=config.SECTION_DEPTH)`
(default 2 path segments) is the canonical record's human-readable
`section` field; `slugify`/`cap_slug`/`disambiguate_slugs` turn a set of
those labels into filesystem-safe names at export time — lowercased,
hyphenated, capped at 60 chars with a content-hash suffix on truncation,
collision-disambiguated with a numeric suffix, and checked against
reserved Windows device names (this repo runs on `D:\scraper`, not a
hypothetical). `plain-jsonl` packaging writes `manifest.json` mapping
each section slug to its derived URL prefix (`crawl/scope.py::derive_prefix`,
reused, not reimplemented) and row count.

**Relevance scoring uses `score_headings`, not the intuitive default
(max-over-chunks)** — chosen from a real measurement, not assumed. See
`LESSONS_LEARNED.md` #17 before changing this: it's the only one of three
candidates that ranked a real API reference page above a marketing page
for a real test intent, and it's also the cheapest. Caveat carried
forward: one intent/one known-correct-page validation, not a robust
multi-query proof.

## Provider routing — one hard invariant

**Local Ollama embeddings must go through the native `LocalOllamaEmbeddings`
class in `llm_factory.py` (a plain `requests.post` to
`http://localhost:11434/api/embeddings`) — never through an
OpenAI-compatible wrapper (`ChatOpenAI`/`OpenAIEmbeddings`-style) pointed at
port 11434.** That wrapper's dynamic-port routing is the exact bug
`LESSONS_LEARNED.md` #3 documents and fixed once already; reintroducing it
would silently break local embeddings again.

Ollama **cloud** is the opposite case for *embeddings* — it has none (no
embedding models exist in Ollama's cloud catalog at all, verified against
docs.ollama.com; see `LESSONS_LEARNED.md` #7) — never build an embeddings
path against it. For *chat*, it's routed through `ChatOpenAI` at
`https://ollama.com/v1` — not confirmed by Ollama's own docs (only the
native `/api/chat` protocol is documented for cloud) but **confirmed
working in practice** via a real end-to-end call with `deepseek-v4-flash`
(`LESSONS_LEARNED.md` #10-11). If it 401s in a future session, check the
loaded env var before suspecting the endpoint (`LESSONS_LEARNED.md` #10
has the exact prior incident). Local Ollama chat has no such doubt either
way — only cloud's OpenAI-compat path was ever in question. Local
embeddings staying off the OpenAI-compat wrapper remains the one hard,
confirmed invariant above.

**Gemini** is a fifth `LLMProvider`, routed through `ChatOpenAI` at
`https://generativelanguage.googleapis.com/v1beta/openai/` — same shape as
every other `ChatOpenAI`-backed provider, no new client class. Chat only;
Gemini does have a working free-tier embeddings endpoint
(`gemini-embedding-2`) but it's deliberately not wired up — see
`config.py`'s `EMBEDDING_CAPABLE_PROVIDERS` comment and
`SYSTEM_ONE_MODELS.md`. Default model is `gemini-3.6-flash`
(`main.py::_DEFAULT_MODEL_BY_PROVIDER`) — live-verified via a real call
after `gemini-2.5-flash` (the docs-picked original default) turned out to
be retired (404, pointed at `gemini-3.6-flash` as the replacement).

**Gemini is now the default extraction provider** (`main.py::_resolve_llm`,
as of 2026-09-22) — chat/extraction only, embeddings are still always
local Ollama per the invariant above. This makes measurements **not
directly comparable across a line in time**: every number in this file and
in `LESSONS_LEARNED.md` up through the Phase 2/3 real-crawl figures, and
the extraction-prompt tuning history in `content/extraction.py`
(table/list-coverage rule, anti-reword rule), was produced against
`ollama_cloud/deepseek-v4-flash`. A different chat model can silently
undo prompt work tuned against a different one — don't assume a rule
verified under deepseek still holds under Gemini without re-checking it
against real output first.

## System One decision models (`decisions/`) — wired, tested against real calls

`USE_SYSTEM_ONE`/`SYSTEM_ONE_PROVIDER` (`config.py`, `.env.example`)
switch every judgment point in the pipeline (branch selection, the
relevance gate, semantic dedup, sample quality scoring) between the
traditional method and a `decisions.DecisionBackend` — `JevBackend`
(hosted, `TYPESAFE_API_KEY`) or `LayaBackend` (local, open-weights,
`uv add laya` — already installed in this environment, numpy<2 confirmed
unaffected). See `SYSTEM_ONE_MODELS.md` for what Jev/Laya actually are,
and `LESSONS_LEARNED.md`'s Steps 2-5 entries for the full build and the
real three-way comparison this recommendation comes from.

**`config.py::validate_system_one_config()`** fails loudly at startup on
a misconfigured `USE_SYSTEM_ONE=true` (bad/missing provider, missing
key, missing `laya` package) — wired into every entry point that makes a
decision (`main()`, `headless_crawl()`, `suggest_selection_command()`).
**No decision-point module reads `config.USE_SYSTEM_ONE`/
`SYSTEM_ONE_PROVIDER` directly** — `decisions.get_backend()` is the one
sanctioned place, enforced by `tests/test_decision_interface_ownership.py`
(AST-based, fails if a future change bypasses it).

**Recommendation, from a real three-way run (manim, 16 branches, two
intents, one direct and one inverted — see `LESSONS_LEARNED.md` #73):**
- **Jev is the recommended `SYSTEM_ONE_PROVIDER` for branch selection** —
  27/32 exact match against the known-correct traditional (70B) answer
  across both intents, including a **perfect** 16/16 on the
  intent-inversion test. Its misses lean toward "uncertain" rather than
  a wrong-signed answer — the safer failure mode by this project's own
  design ("Uncertain is a real answer, not a guess").
- **Laya is not viable zero-shot for branch selection.** 11/32 overall,
  and the failure on the inverted intent is **structural, not a
  threshold issue**: it selected 13 of 16 branches regardless of
  relevance, with an irrelevant branch (`reference/`, 0.90) scoring
  *higher* than the one actually about the intent (`changelog/`, 0.85).
  No threshold recovers a backwards ordering — this matches Laya's own
  documented Honest Limit ("base checkpoints are near chance zero-shot
  on typed-decisions... treat Laya as a fast base to specialise, not a
  zero-shot decision engine"), confirmed live, not just assumed from
  its docs.
- **Neither is yet recommended for semantic dedup** — on the two
  known-hard false positives from `LESSONS_LEARNED.md` #40, Jev was the
  best performer on one case and the *worst* (more confidently wrong
  than plain string similarity) on the other. Small sample (2 pairs);
  a wider hand-labeled comparison is tracked as follow-up work, not
  concluded yet either way.
- Sample quality scoring: untested against real calls as of this entry
  — no real call made yet, not assumed to inherit branch selection's
  result.

**`--verify-providers`** (`main.py`) pings every `LLMProvider`'s default
model with one real call and reports which respond — run this before
trusting any default picked from docs, not after. It has already caught
four wrong defaults this way (nvidia, ollama_cloud, gemini, and
local-ollama's `llama3` — resolvable in Ollama's public registry but
never actually pulled on the machine it was tested from); see
`LESSONS_LEARNED.md` for each. A provider whose key isn't in `.env` is
reported as `skipped`, never conflated with a pass.

## Queue architecture invariants

Crawling is a bounded work-queue pipeline (`crawl/frontier.py` + crawl
workers + `content_queue` + extract workers + `results_queue` + one writer
task), never recursive task-per-page spawning. Full state machine and
schema live in `crawl/frontier.py`'s module docstring — this section
covers the invariants that matter when touching it.

**Locking is structurally two-layered, not just documented.** Every
`Frontier` method named `_locked_*` assumes `self._lock` is already held and
must never acquire it, and must never call a public (non-`_locked_`) method.
`asyncio.Lock` is not reentrant — either violation is an instant permanent
deadlock, not a slow path. Public methods acquire the lock, delegate to
`_locked_*`, release.

**What's allowed inside the lock, stated precisely** (an earlier draft of
this rule said "no awaits in the critical section," which is now wrong and
would read as license to relax further if left as-is): awaits inside the
lock are permitted **only** for calls on the shared `aiosqlite` connection.
Never a queue operation, a network call, an LLM call, or a `sleep` — those
can block for unbounded time, and `content_queue`/`results_queue` are
bounded for backpressure, so `await queue.put()` specifically can block
indefinitely. Holding the lock across that stalls every other worker,
including the ones that would drain the queue and unblock it — a full
deadlock. `put_content`/`put_results` in `crawl/frontier.py` release the lock
*before* the `await queue.put()` for exactly this reason; keep that pattern
for any new put-site.

**Termination is global quiescence, not a per-stage sentinel cascade.** An
extract worker promoting `pending_score → queued` means extraction feeds
back into the frontier — "crawl stage idle, nothing queued" is not a safe
place to shut the crawl stage down on its own, since a slow extract worker
still mid-flight is about to produce more claimable work. The run ends only
when `in_flight == 0` (a single counter, incremented on claim/content-queue-put/
results-queue-put, decremented on leaving `in_progress`/task_done) **and**
either zero `queued` rows remain or `max_pages` has been reached. See
`tests/test_frontier_quiescence.py` for the regression this guards against —
a slow extract worker relative to a fast crawl worker causes multiple
genuine idle-then-resume cycles mid-run; a cascade design stops after the
first one.

**`max_pages` leaves `queued` rows live in the frontier by design, not by
accident.** When the cap trips, claim() stops returning rows even though
some are still `queued` — they're never touched again *this run*. A resumed
run against the same frontier DB will continue claiming them, since nothing
marked them `failed` or `skipped_follow`. This means `max_pages` is a
per-run fetch budget, not a per-database ceiling — surprising if you expect
it to cap total pages ever processed against this frontier across restarts.
Relevant to any test that resumes a capped run (step 8 exercises both the
capped-stop path and the resume-past-cap path in one test).

**What counts toward `max_pages`** is one shared predicate
(`Frontier._budget_sql`), used by both `_locked_claim` and
`_locked_check_quiescence`. Never write the status list out separately
again: if the two disagree, a run can stop claiming without ever
quiescing. By default only `done`/`skipped_extract`/`mapped` count.
With `config.MAX_PAGES_COUNTS_FAILED` (off by default), terminally
`failed` rows count too, except a robots.txt disallow
(`ROBOTS_DISALLOWED_ERROR`; no request was made). Without it, a run
whose pages keep failing never reaches its budget (`ROADMAP.md` #54).
Like done rows, failed rows already in a resumed `frontier.db` count.

**JSONL duplicate-write risk on crash-resume, and why it's handled at the
file layer, not a DB column**: see `LESSONS_LEARNED.md` for the incident
this traces to. Short version — `status='done'` is set by the *writer*,
only after the JSONL append and Chroma upsert both succeed, so a crash
between "extraction finished" and "writer wrote it" correctly leaves the row
`in_progress` (retried on resume) rather than falsely `done`. But that retry
re-runs extraction and re-appends to JSONL — and a same-transaction SQLite
"written" marker can't fix this, because it would be part of the same
transaction as the status update, which is exactly what didn't commit. The
JSONL file's own `source_url` field (scanned at startup, same pattern as the
existing instruction-text preload) is the actual source of truth for "was
this URL's content already persisted" — it survives independently of
whatever the frontier's status column says.

## Map mode, cancellation, graph projections, live feed (pre-UI backend work)

Four CLI-usable-on-their-own capabilities, added before any UI, each with
its own tests (`tests/test_map_worker.py`, `tests/test_cancel_subtree.py`,
`tests/test_graph_projections.py`, `tests/test_live_status.py`); see
`LESSONS_LEARNED.md` #59 for the design decisions and what was measured.

**Map mode** (`uv run python main.py --map <url>`, `crawl/pipeline.py::map_worker`):
a discovery-only pass — fetch, discover + scope-filter links, record the
frontier — with no relevance scoring, no LLM calls, no chunking. Reuses
`crawl_worker` unmodified; only the extract stage is replaced. Every fetched
page lands as `mapped`, a terminal status kept deliberately distinct from
`skipped_extract` (which means the relevance gate *did* run and reject the
page — conflating the two would make a map run's `counts_by_status()` read
as a mass rejection). Content is never persisted (same as a normal
extract pass — it only ever lived in `content_queue`), so resuming means
re-fetching, not resuming from saved text. Workflow: map, then
`--select`/`--select-prefix` (`Frontier.requeue_for_extraction`) promotes a
chosen subtree's `mapped` rows back to `queued`, then a normal
`python main.py` run against the same `frontier.db` re-fetches and actually
extracts just that subtree. Anything never selected stays `mapped` forever
— no need to explicitly cancel what wasn't selected.

**Cancellation** (`--cancel <url>` / `--cancel-prefix <prefix>`,
`Frontier.cancel`): a new terminal `cancelled` status. Cancels every
row reachable from the target(s) via `parent_url`, in one transaction,
touching only rows currently `pending_score`/`queued`/`in_progress` —
already-terminal rows are left alone. **In-flight decision, made and
documented on `Frontier.cancel`'s docstring, not left implicit**: there is
no mechanism to abort an in-flight crawl4ai fetch or LLM call mid-request
(same conservative posture as `BrowserDriverError`'s fail-fast design), so
a claimed row keeps running and overwrites its own `cancelled` status with
its real outcome once it finishes naturally. What actually stops the
subtree from regrowing through that node: `_locked_insert_children` checks
the parent's *current* status before inserting anything, so a node
cancelled while in flight never gets its children (or their
`discovery_edges` rows) written once its fetch completes.
`requeue_for_extraction` walks the same tree but explicitly stops at a
cancelled node instead of through it — selecting an ancestor for
extraction must not silently resurrect a subtree that was deliberately
excluded underneath it.

**Graph projections** (`--graph {discovery,path-tree}`, `crawl/graph.py`,
pure reads over a fresh sqlite3 connection — never the async `Frontier`):
`discovery_graph` (edges = `parent_url`, the literal shape the crawl found
things in) and `path_tree` (grouped by URL path segments, "what a site
looks like"; off-seed-host URLs are reported flat in `cross_host_nodes`
rather than forced into a path hierarchy for a site the crawl never
explored). Both support a subtree-scoped/depth-limited query
(`--graph-root`/`--graph-max-depth`) for progressive loading at scale, and
report `depth`/`child_count`/`subtree_size` per node (computed at query
time, one linear in-memory pass, not stored) as layout inputs for a future
renderer. `url` is the stable node id across polls — no surrogate key.
**`frontier.parent_url` only ever records a URL's first discoverer**
(`INSERT OR IGNORE`); a `discovery_edges(parent_url, child_url,
discovered_at)` table (populated by every `insert_children` call, schema-
additive, no migration) is what makes real inbound-link counts
(`inbound_count` per node, `multi_inbound_link_count` overall) possible.
A `frontier.db` written before this table existed reports
`edges_tracked: false` and `multi_inbound_link_count: null` — explicitly
unknown, never a misleading `0`; there is no way to reconstruct those
missing edges after the fact.

**Live feed** (`--live-status`, `crawl/live_status.py::read_live_status`):
a poll-friendly, read-only snapshot — status counts, in-flight rows (each
with `parent_url` and `running_seconds`, computed as `now - updated_at`
since nothing else touches an in-progress row's `updated_at` between
`claim()` and whatever `mark_*` call ends that claim), recently-completed
rows, and the timestamp of the last change. Opens its own short-lived
sqlite3 connection — safe to run from a different process against a
`frontier.db` a crawl is actively writing to (WAL mode). No push mechanism
yet (`ROADMAP.md` #43) — poll again for a fresh snapshot.

**Pause** (`--pause`/`--unpause`, `Frontier.set_paused`/`is_paused`): a
single-row `control` table (`paused` flag), not a Python attribute —
pausing is initiated by a *different OS process* (the control server, see
below) than the one running the claim loop, so only the DB is actually
shared between them. `_locked_claim` checks it before claiming (returns
`None`, rows stay `queued`); in-flight fetches/extractions are never
aborted, only new claims stop. **`_locked_check_quiescence` explicitly
checks paused first and returns immediately if set** — not just a
corollary of the queued-count check below it, a deliberate, separate
guard, because a paused run can genuinely reach `in_flight==0` (every
in-flight page finishes naturally) while queued rows sit untouched, which
is exactly quiescence's own trigger condition. This is the third instance
of "a state nothing moves out of" breaking quiescence (`LESSONS_LEARNED.md`
#44, and the cancellation work in #59) — caught and tested (`pause with
queued rows and zero in-flight must not quiesce`) before it ever reached a
live run, then confirmed against one anyway (#61): pausing mid-crawl froze
`counts_by_status` completely across three `--live-status` polls, job
stayed `running` the whole time, and unpausing resumed immediately.

## Headless contract v1 (`headless.py`, Brief 3A)

`--headless-config` has two contracts, chosen by one key.
- **No `contract_version`: legacy headless, exactly as before.** This is
  what the control server sends (with an extra `root_url` key, ignored).
  Never make legacy stricter: that breaks the server.
- **`"contract_version": 1`: strict v1.**
  - Unknown keys are refused, all listed by name.
  - Behaviour settings (`extract_unit_concurrency`,
    `max_pages_counts_failed`, `llm_breaker_threshold`,
    `llm_sdk_max_retries`, `use_system_one`, `system_one_provider`,
    `extract_timing_log`) go in a `"settings"` object, resolved **config
    value > environment variable > default**, with each value's source
    recorded.
  - `headless.applied_settings()` writes them onto the `config` module for
    the run and restores them after. That's safe because every reader uses
    `config.X` at call time: keep it that way, and don't import these
    settings by name.
  - `"api_key_env"` names the environment variable holding this run's key.
    The value goes only into `get_llm(..., api_key=)`, never into the
    config dict, `result.json`, errors or logs.
  - Any other version is a config error.
- **v1 safe defaults (Step 4, item 18)**, v1 only. `config.py` and legacy
  keep today's values; a config value or env variable can still turn each
  one off.
  - `max_pages_counts_failed`: on.
  - `llm_breaker_threshold`: 5. This also forces SDK retries to 0.
  - `site_retry_backoff`: on. A site's 429/503 re-queues the page after the
    site's `Retry-After`, else 5s doubling per retry, capped at 300s. It
    still counts as a retry.
  - `llm_unavailable_max_releases`: 5. An LLM 500/502/503/504 releases the
    page with the provider's hint or 5s doubling, **without** using a
    retry, at most 5 times per page per run (counted in memory). The next
    one fails the page: "gave up after 5 releases".
  - **The breaker only sees 429s.** A 5xx neither trips it nor resets its
    count. With the breaker on, the SDK no longer retries 5xx either, so the
    release path is what absorbs a provider's 503 spike.
- **`SCRAPER_LOAD_DOTENV=0`** (`config.py`): skip `web-scraper/.env`
  entirely. foundry sets it and passes settings in the config and the key
  via `api_key_env`. Unset or any other value: `.env` loads as before.
- **v1 exit codes** (`headless.EXIT_*`): 0 ok, 1 stopped early
  (`fatal_errors`), 2 config error (one line on stderr, no traceback),
  130 SIGINT, 143 SIGTERM. Legacy keeps 0 / 1, and Ctrl-C there still
  exits 0.
- **v1 run identity and outputs (Step 2):**
  - **`run_id`:** from the config, else generated.
  - **`result.json`:** written atomically for every outcome: `ok`,
    `stopped_early`, `interrupted`, `crashed`, and `config_error` when the
    run_dir exists and holds no earlier run (**never clobber an earlier
    result**, and never create a missing run_dir). It holds the contract
    version, run id, scraper version (git commit + dirty flag, else
    `0.1.0+unknown`), outcome and exit code, effective settings with
    sources, counts, fatal errors, the key variable's name and set/not-set,
    and for a crash the error type plus a message with paths and keys
    removed. The same result is the stdout line, even if the file can't be
    written.
  - **Resume:** a run_dir holding scraper state (`frontier.db`,
    `canonical.jsonl`, `timings.jsonl`, `result.json`; other files don't
    count) is refused unless `"resume": true`, and `resume` with nothing to
    resume is refused. On resume the old result is kept as
    `result.<previous run id>.json` and the new one records
    `resumed_from_run_id`.
  - **Stamps:** timing events carry `run_id`, and canonical rows carry
    `scraper_version`, in v1 only. Legacy output is byte-identical, so pass
    `None` for both outside v1.
- **Graceful stop, v1 crawl runs (Step 3, `crawl/graceful.py`):** handlers are
  installed only when a v1 `RunContext` is present. Legacy and map runs keep
  the default signal behaviour.
  - **First SIGTERM/SIGINT:** the frontier stops handing out rows
    (`begin_drain`, in memory only). Claimed pages get
    `settings.stop_grace_seconds` (default 30) to finish and be written.
    Then the workers are cancelled, rows still claimed go back to `queued`
    without a retry (`release_in_progress`), and leaving the
    `AsyncWebCrawler` context closes Chromium. Exit 143 / 130.
  - **Second signal:** a minimal `result.json` (`stopped_immediately`),
    then `os._exit`, with no cleanup.
- **Canonical rows are appended one page per `O_APPEND` write plus fsync**
  (`storage/writer.py::append_page_atomically`), in every mode. The old
  text-mode `writelines` left a partial page in 39 of 80 measured SIGKILLs,
  and `Writer` then treats that URL as written on resume, so the missing
  rows are lost for good. On a v1 resume, a cut tail (still possible in
  principle) is moved to `canonical.tail.<run_id>.bin`, never deleted.
- **Leftover browser processes and recovery** (measured on macOS with real
  Chromium, `LESSONS_LEARNED.md` #89):
  - **Graceful stop:** nothing is left behind.
  - **After a second signal:** Playwright's Node driver closes the browser
    itself within about 0.25s once it sees its parent gone.
  - **Real leftovers only happen if the driver is killed hard** (SIGKILL to
    the group). Chromium runs in its **own process group**, so a group
    signal never reaches it, and it can outlive the scraper.
  - **Recovery:** the launcher must record the scraper's descendants (PID +
    creation time) *before* signalling, then kill survivors by those
    recorded PIDs, matching the creation time. foundry's `procs.stop` does
    exactly this. **Never kill by name or command line**: `pkill chrome`
    would also kill the user's own browser. Without a recorded tree, inspect
    (`ps -o pid,ppid,pgid,lstart,command`) and kill one confirmed PID at a
    time.
  - **Signal the scraper's PID first, not its group.** A group SIGTERM
    reaches the driver at the same moment, Chromium closes under in-flight
    fetches, and each of those pages uses up a retry (measured: 2 rows at
    `retry_count` 1). Launchers should send SIGTERM to the PID, wait for the
    grace period, and only then signal or kill the group.
- **Slow tests: `RUN_SLOW_TESTS=1`.** The real-signal tests in
  `tests/test_graceful_stop.py` (subprocess runs with real SIGTERM/SIGINT,
  and the SIGKILL kill-mid-append loop, about 70s) are skipped by default.
  Run `RUN_SLOW_TESTS=1 uv run python -m unittest discover -s tests` before
  merging anything that touches headless, signals or the writer.
- **Network guard (fast suite):** `tests/test_000_network_guard.py`, which
  discovery imports first, installs `tests/netguard.py`.
  - **What fails a test:** any non-localhost connection or DNS lookup, and
    any Playwright/Chromium launch.
  - **Where it applies:** in the test process, and in every child Python
    (via `tests/netguard_site/sitecustomize.py` on `PYTHONPATH`).
  - **Swallowed violations count:** the test fails even if the code caught
    the exception.
  - **Opting out:** mark a test `@allow_network`, which is only for the slow
    tests: `RUN_SLOW_TESTS=1` real-signal tests and the two real-Chromium
    `raw:` HTML fetch tests.
  - **Single-module runs** (`-m unittest tests.x`) aren't guarded unless that
    module imports the guard.
- **v1 and legacy rows are byte-checked:** `tests/test_v1_golden.py` runs a
  deterministic headless run and compares `canonical.jsonl` byte-for-byte
  with `tests/golden/*_main_1112ad7.jsonl`, produced by
  `tests/golden/make_rows.py` from a `git archive` of pre-3B `main`.
  Never regenerate them from the branch under test.
- **Tests that apply settings must restore `config`.**
  `tests/test_headless_contract.py`'s `ConfigGuard` fails a test that
  leaves it changed.

## Headless contract v2 (Brief 3B, in progress on `brief-3b-data-engine`)

`"contract_version": 2` = every v1 key plus `headless.V2_KEYS`. A v1
config using a v2 key is refused as unknown, exactly as before; v1 and
legacy runs take today's code paths (the v2 branches are all behind
`context.contract_version >= 2`).
- **Step 1 (done):**
  - **Rows:** v2 rows are canonical schema v2
    (`storage/canonical.py::build_canonical_record_v2`): `schema_version`
    2, `record_type`, the same top-level fields (so exporters still find
    `question`/`answer`), and a `provenance` block: generator, provider,
    model, pinned version (`model_pinned_version`, default the model),
    `served_model`, endpoint, scraper version, run id, prompt sha256.
  - **`served_model`:** the provider response's own `model` field, captured
    by `llm_factory.ServedModelChatOpenAI`. langchain's `model_name` falls
    back to the *requested* model, so never read it as provenance.
  - **Source records** (`crawl/sources.py`) go to `sources.json` and into
    `result.json`, in foundry's `data_sources` shape. A missing value is the
    explicit `none_found`. Licence is evidence (snippet + page URL), never
    a legal verdict. AI-training signals are reported, not enforced.
  - **Request form vs identity (ROADMAP #53):** v2 checks robots and
    fetches the URL in the form it was first discovered (trailing slash
    kept, `scope.fetch_form`). Identity stays `normalize_url`. The mapping
    lives in a v2-only side table, `fetch_urls`.
- **Step 2 (done):**
  - **Preflights** (`preflight.py`): they run before any page work, behind
    the v2-only settings `provider_preflight` and `embeddings_preflight`.
    - **Provider:** one tiny call, only when the run will make calls. A
      retired model (410, or a 404 naming the model) or a rejected key
      (401/403) raises `PreflightFailed`: `config_error`, exit 2. Anything
      else is a recorded warning.
    - **Embeddings:** only when the run needs them (intent, RAG,
      top_k). Any failure fails the run, and the message lists remedies.
  - **Dry pass (`dry_run: true`):** the chat provider is never constructed.
    `estimate_worker` makes extract_worker's score and follow decisions,
    runs the real chunker, and marks pages `mapped`. It writes
    `estimate.json`, whose token basis is in `config.ESTIMATE_*`, measured
    from the Brief 2c run. A dry pass can't be resumed into a real run, and
    can't be combined with `resume`.
  - **Cost** (`crawl/cost.py`): `result.json` has a `cost` section counting
    every call attempt by outcome, per provider, model and role, plus 5xx
    releases. A token total is null when no call reported it, never 0.
  - **Fingerprints:** `estimate.json`'s `page_fingerprints` holds each page's
    content SHA-256 and each unit's SHA-256 and length. They tell "the page
    changed" apart from "the estimate is wrong".
- **Step 3 (done): source balancing** (`content/balance.py`), v2 only.
  - **Settings** (`headless.SETTINGS_V2_ONLY`): `max_rows_per_page` (100),
    `max_rows_per_source` (null = off), `low_value_policy` (`cap`; or
    `skip` / `keep`), `low_value_max_rows` (25). Null turns a cap off, from
    the config or as `none` in the environment.
  - **Overrides key:** `low_value_overrides` `{"keep": [...], "skip":
    [...]}` takes URL or path prefixes. The longest matching prefix wins,
    and a tie keeps.
  - **Caps are per record type.** The keep rule is round-robin across a
    page's units, and kept rows stay in their original order.
  - **The per-source cap** is applied in the order pages finish, which
    varies between runs, so every cut is recorded.
  - **Low-value heuristic:** URL segment, title, version-number headings,
    or link-line share, with config values in `config.LOW_VALUE_*`. The
    reasons are recorded.
  - **`skip`** marks the page `skipped_extract` before any call, and the dry
    pass skips it too.
  - **`result.json`'s `balance` section:** rows per page, the largest page
    and its share, rows per source, every cut, the low-value pages and the
    skipped pages.
  - **Offline recompute:** `rebalance_rows()` runs the same logic over an
    existing `canonical.jsonl`.
- **Licence evidence is site-level only** (ROADMAP #63): `rel="license"`, a
  link to a licence page or file, or a licence name in footer text. HTML
  comments, `<svg>`, `<script>`, `<style>` and body text never count. The
  HTML parse (`crawl/sources.py::page_evidence`) runs in `asyncio.to_thread`;
  only its result is recorded on the event loop.
- **Step 4 (done): pluggable generators** (`generators/`), v2 only.
  - **Config key:** `generators`, a list of `{name, version, options?}`.
    The default is `[{"name": "qa", "version": 1}]`. An unknown or
    duplicate generator, or any option, is a config error.
  - **Single-call interface:** `wants(unit, page, i)` (cheap filter),
    `request(unit, page, i)` returning `(system prompt, text)`, and `parse(raw,
    unit, page, i)` returning a list of field dicts. `parse` raises
    `MalformedExtractionError` when it can't read the response.
  - **The worker makes every call:** `extract_worker`'s `guarded_call`
    applies the breaker gate, the unit semaphore, 429/5xx handling, cost and
    timing for v1's call and for every generator's. A generator can't make
    an ungated or uncounted call.
  - **Labelling:** cost rows, timing events and 5xx releases carry the
    generator id.
  - **`PageContext`** holds url, title, section and every unit of the page.
  - **`qa@1`** sends the provider exactly v1's messages (tested). Its v2
    rows equal the golden v1 rows field for field, once the header and
    provenance are removed.
  - **`code_example@1`** calls only on units with a fenced block. It sends
    up to `config.CODE_EXAMPLE_CONTEXT_CHARS` of the preceding unit as
    context, because the chunker often leaves a code block alone in its
    unit. Its rows have `instruction`, `code`, `language` and `explanation`.
  - **Malformed answers:** a unit counts as malformed only when every
    attempted generator's answer was unreadable.
  - **Quality scoring** applies only to `qa` rows.
  - **Dry pass:** `estimate.json` gives calls, input characters and tokens
    per generator. A non-`qa` output ratio is labelled "unmeasured".
  - **Exporters** skip non-`qa` rows and count them in the dataset card
    (`skipped_non_qa_rows`, present only when something was skipped).
- **Step 5 (done): verifier hook** (`verifiers/`), v2 only.
  - **Config key:** `verifiers`, a list of `{name, version, on_fail:
    "drop"|"flag", options?}`. The default is `schema@1` with `flag`, and
    `[]` turns verification off: rows then carry no verification fields.
  - **Order in the extract path:** generation, then verification, then
    balancing, then the writer. Caps count only rows that survived, and
    `commit()` sees only rows actually written.
  - **On rows:** `verification` (one `{verifier, version, verdict,
    reasons}` per verifier that applied, in config order) and
    `verification_status`.
    - The status is `fail` if any verifier failed, else `unknown` if any
      was unknown, else `pass`. It's `unverified` when no verifier applied.
    - `drop` removes a row that fails, and `unknown` never drops. A
      verifier that raises gives `unknown`.
  - **In `result.json`:** a `verification` section with pass, fail,
    unknown and dropped counts per verifier, and written rows by status.
  - **`schema@1`:** checks the generator's JSON Schema, plus the export
    validation rules for `qa`. For `code_example`, the code must appear,
    with whitespace collapsed, in the EXACT text sent. That text is
    `CodeExampleV1.sent_text(row)`, rebuilt from the row's
    `source_chunk`, its stored `context` and its title, and capped as the
    worker caps it.
  - **`near_duplicate@1`:** order-dependent by design. Within a page the
    earliest row in chunk order wins; across pages, the order pages finish.
  - **`code_syntax@1`:** parses only; nothing is executed.
  - **`external@1`:** the row goes in as JSON on stdin and a JSON verdict
    comes back on stdout.
    - **How it runs:** an argv list with no shell, its own process group,
      an empty temporary cwd that's removed afterwards, and an environment
      of PATH and LANG only (plus HOME/TMPDIR set to the cwd), so no keys.
    - **Limits:** stdout capped at
      `config.EXTERNAL_VERIFIER_MAX_STDOUT_BYTES`, a timeout, and at most
      `settings.external_verifier_concurrency` processes at once.
    - **Failures:** on a timeout or oversized output, the group is killed by
      the child's own PID. A non-zero exit or garbage output is `unknown`.
    - **An external verifier that runs generated code must sandbox it
      itself.** The isolation above is not a sandbox.
- **Tests that spawn the real `main.py`** must use a config that can't run
  (an unsupported version, a never-existing run_dir). When version 2
  became valid, one such test silently ran a real v2 crawl
  (`LESSONS_LEARNED.md` #91).

## Control server (`server/`)

Local HTTP control layer — `uv run python server.py`, Flask,
**localhost-only (`127.0.0.1`), no authentication**. Every mutating or
LLM-touching operation launches as a separate `python main.py ...` /
`python -m export.export ...` process (`server/jobs.py::JobManager`) —
the server itself never runs crawl4ai, never opens the async `Frontier`,
never touches chromadb, for the same event-loop-starvation reasons a crawl
must never share a process with anything else that needs to stay
responsive. Read-only endpoints call `crawl/graph.py`/`crawl/live_status.py`
directly (already safe, already read-only, already used from other
processes by design).

Job tracking is a PID file per run (`data/<run_id>/job.json`) plus the
process's real exit code, captured through a thin wrapper around every
launched command rather than guessed from stdout shape — `_final_summary`
tried sniffing for one specific JSON shape first and got it wrong the
moment a second job kind (`export.py`, which prints an unrelated
`dataset_card`) came along; see `LESSONS_LEARNED.md` #61 for the full
incident list (five real bugs found only by running the actual headless
demo, not by review). `JobManager` is genuinely stateless beyond that —
every status query re-derives from the file + a live OS check
(`tasklist`/`taskkill` on Windows), never from anything cached in memory,
so state survives a server restart with no reconstruction step needed.

See `README.md`'s "Control server" section for the full endpoint list.

## Event loop invariant: no blocking I/O, ever, in any `async def`

Every worker shares one `asyncio` event loop, and the crawl side shares
one `AsyncWebCrawler` (one Playwright `BrowserManager` underneath —
confirmed by reading `crawl4ai`'s own source, not assumed). A single
`async def` that calls a **synchronous** DB, disk, HTTP, or embedding
call directly — no `asyncio.to_thread` — blocks that one shared loop for
however long the call takes. Nothing else scheduled on it runs
meanwhile, including crawl4ai's own async communication with its
browser driver: a loop starved long enough breaks that connection, and
because the browser is shared, it breaks it for *every* crawl worker at
once, not just the caller. A real crawl hit this: `storage/chunk_store.py`'s
`ChunkStore.add_or_merge_chunk()` called `collection.get()`/`add()`/
`update()` (chromadb's client is synchronous) directly on the loop.
Confirmed by isolation, not just by reading the code — extraction-only
load ran 5+ minutes clean; the same load with extraction stubbed out and
only this RAG/Chroma path active froze the whole pipeline solid within
~20 seconds. See `LESSONS_LEARNED.md` for the full incident and fix.

**The rule, stated generally**: no `async def` may call a blocking
library function without wrapping it in `asyncio.to_thread`. Two
precedents in this codebase, one on each side of the line —
`aiosqlite` (frontier.py's DB layer) does this internally, transparently,
which is why it was never a problem; `chromadb`'s client does not, which
is why `storage/chunk_store.py::add_or_merge_chunk()` and
`storage/query.py::query_chunks()` (same defect, lower blast radius —
`--query` never runs alongside the concurrent pipeline, fixed anyway
since the invariant is unconditional, not "only where it currently has
company to starve") both needed an explicit `asyncio.to_thread` wrap.
Before adding any new I/O call inside `async def`, confirm which side of
that line the library is on — don't assume "it's a client library"
means it's already safe.

`asyncio.to_thread` is sufficient without extra locking **only** where
the call site already has exactly one caller at a time — true for
`ChunkStore` today because `storage/writer.py::Writer.write()` is its
sole caller and already refuses concurrent calls (its own `_guard`, see
that file). A blocking call reachable from more than one concurrent
task would need actual serialization (a lock), not just a thread hop —
check this before assuming `to_thread` alone is enough anywhere new.

**Shared-browser death has its own fail-fast path, as a second,
independent layer** — fixing the starvation doesn't guarantee nothing
else ever kills the shared driver. `crawl/pipeline.py::BrowserDriverError`
is raised (via a message-substring match in `main.py::_make_fetch_fn`)
when crawl4ai reports the specific "connection closed while reading from
the driver" failure; `crawl_worker` does not retry it (retrying against
the same dead shared browser is a guaranteed repeat, not a transient
failure) — it marks the row permanently failed, sets `frontier.quiescent`
directly, and returns. Every worker (including siblings already mid-loop)
picks this up via the *same* quiescent check they already poll, checked
now at the top of the loop too, not just in the row-is-None branch, so a
sibling doesn't burn one more row into the dead browser before noticing.
`main()` reports this distinctly (`Stopped early: ...`, non-zero exit)
rather than printing `Done.` as if the run completed normally. Chosen
over attempting to rebuild/reconnect the shared browser mid-run: this
project doesn't have evidence a reconnect would actually recover cleanly
mid-flight, and a loud, fast, clearly-diagnosable stop is safer than a
silent one that either recovers via unverified reconnect logic or keeps
burning the frontier on a mechanism nobody's confirmed works.

## Scope predicate (`crawl/scope.py`)

`normalize_url()`, `derive_prefix()`, and `is_in_scope()` are pure
functions, no I/O — `crawl/pipeline.py`'s `crawl_worker` and
`main.py --dry-run` both call them, and they're unit-tested offline
against real fixture data from 5 structurally different sites in
`tests/fixtures/` (see `tests/fetch_fixtures.py` to regenerate). **No
site-specific logic belongs in `crawl/scope.py`** — if a site needs different
behavior, it has to
come from config (host allowlist, prefix list, exclude patterns), never a
branch on a domain name inside the predicate. `derive_prefix` has already
had one real bug from over-fitting to a single test site (a universal
`dirname()`-before-`commonpath()` heuristic that broke on any branch
containing its own section-index page — see `LESSONS_LEARNED.md` #8);
treat that as the standing warning it is before changing this file.

## Worker loops (`crawl/pipeline.py`, `storage/writer.py`, `content/extraction.py`, `crawl/robots_cache.py`, `progress_display.py`)

`crawl_worker` / `extract_worker` / `writer_worker` in `crawl/pipeline.py`
are the real implementations of `crawl/frontier.py`'s design — built and
tested against stubs (`tests/stub_fetcher.py`, inline stub
`score_fn`/`extract_fn` in the test files) before any of them ever touched
the network, and proven against a real crawl in step 8 Part D (real
extraction, real RAG index, real crash-resume).

- **Every I/O dependency is injected** (`fetch_fn`, `score_fn`,
  `extract_fn`, `Writer`) — this is what makes offline testing possible at
  all, not an incidental design choice. Keep new worker logic testable the
  same way; don't reach for the real crawl4ai/LLM client from inside a
  worker function directly.
- **A retry re-fetches, not just re-extracts** (see the state machine —
  `in_progress` spans both sub-phases). A test that exercises a retry path
  needs something claiming `queued` rows and re-supplying content, not
  just re-running the failing stage in isolation — see
  `tests/stub_fetcher.py`-adjacent `recrawl_stub` helpers in
  `tests/test_extract_worker.py` / `tests/test_crawl_worker.py`, and
  `LESSONS_LEARNED.md` #14 for what happens when a test forgets this (it
  hangs, it doesn't fail fast).
- **Malformed LLM JSON**: `content/extraction.py::parse_qa_json` tries direct
  parse, then a stripped code fence, then the substring between the first
  `[` and last `]` (salvages prose-wrapped JSON, the common case) before
  raising `MalformedExtractionError`. An empty-but-valid parse (`[]`) is
  not an error — don't conflate "the LLM found nothing" with "the
  response was unparseable," they need different handling upstream.
- **Rate limits release the row, they don't sleep the worker.**
  `RateLimitError(retry_after=...)` → `frontier.mark_extract_outcome(...,
  backoff_seconds=...)` sets `not_before` and hands the row back to
  `queued`; the worker moves on immediately. A worker `sleep`ing in place
  for a 429 blocks that concurrency slot for the full backoff — with a
  small pool, a few 429s idle the whole stage. **The backoff is the
  provider's own hint when it sends one**: every raise site
  (`main.py`'s three `_make_*_fn`s, `decisions/jev.py`) builds the error
  with `RateLimitError.from_provider_error()`, which reads
  `retry-after-ms`/`Retry-After` or Google's body `retryDelay`, plus a
  `quota_hint` (Google `quotaMetric`/`quotaId`) and a capped,
  secret-redacted `detail`. `last_error` becomes "rate_limited (stage):
  <hint>", and the timing log carries the details. The flat
  `DEFAULT_RATE_LIMIT_BACKOFF_SECONDS` applies only when there's no
  hint, and every backoff is capped at
  `config.RATE_LIMIT_MAX_BACKOFF_SECONDS` (a per-day "retry in 20h"
  would otherwise park a queued row so far out that the run never
  reaches quiescence).
  **The one deliberate exception: the LLM circuit breaker**
  (`crawl/circuit_breaker.py`, `config.LLM_BREAKER_THRESHOLD`, **off by
  default**). With it on, after N consecutive rate-limited LLM calls,
  extract workers **wait at the breaker's gate** before any new
  extraction or quality call, holding their pages instead of releasing
  them. The release rule exists so one row's backoff doesn't idle a
  worker while other work could proceed. With the breaker open, no LLM
  work can proceed at all, so waiting is the point: it stops the "every
  429 releases a page, the next page 429s too" spiral (`ROADMAP.md`
  #54, `LESSONS_LEARNED.md` #81), keeps finished units, and doesn't burn
  retry budgets.
  - **Unaffected:** a sporadic 429 while the breaker is closed still
    takes the normal release-the-row path.
  - **The gate:** permits are taken *outside* the unit semaphore, so a
    waiting call holds no slot. After the cooldown (the provider's own
    hint, else 60s doubling, capped at 900s) one probe call decides.
  - **Stopping:** after `LLM_BREAKER_MAX_TRIPS` trips, or at once on a
    daily-quota signal, the run stops the way a dead browser stops it:
    a message in `fatal_errors`, quiescence set, non-zero exit, rows left
    resumable, never marked failed.
  - **The daily signal is decided from the UNCAPPED provider hint**
    (`RateLimitError.retry_after`/`quota_hint` as reported), never from
    the 300s-capped row backoff: a quota hint naming a per-day quota, or
    a hint of at least `LLM_BREAKER_STOP_IF_RETRY_AFTER_SECONDS`.
  - **It never cancels in-flight calls.** They run in `asyncio.to_thread`,
    which can't be cancelled (`ROADMAP.md` #58). They finish and report
    their outcomes, and only a probe's failure can re-trip.
  - **SDK retries:** with the breaker on, `ChatOpenAI`'s SDK-level
    `max_retries` (`config.LLM_SDK_MAX_RETRIES`, default 2, unchanged) is
    forced to 0, so every 429 reaches the breaker instead of being
    silently retried up to twice first.
- **`Writer` (in `storage/writer.py`) has exactly one caller: `writer_worker`.**
  `crawl_worker`/`extract_worker`'s signatures structurally cannot receive
  a `Writer` or Chroma client (enforced by
  `tests/test_writer_ownership.py`, which fails if either one ever grows a
  parameter with "writer" or "chroma" in its name) — this is what lets
  `Writer` skip internal locking entirely and instead assert loudly
  (`RuntimeError`) if it's ever called concurrently, rather than silently
  serializing calls the way the old `asyncio.Lock`-per-write pattern did.
- **robots.txt is respected, including for an explicitly-selected
  branch** — `crawl_worker` checks `RobotsCache` before every fetch;
  disallowed URLs go straight to `failed` (`mark_permanently_failed`, no
  retry) with a loud print, never silently dropped or silently crawled
  anyway. `crawl/robots_cache.py` resolves Allow/Disallow itself (a
  from-scratch RFC 9309 longest-match implementation) rather than
  delegating to stdlib `urllib.robotparser`, which resolves by file order
  instead of specificity and broke a real site's robots.txt — see
  `LESSONS_LEARNED.md` #27 before assuming a "respects robots.txt" claim
  extends to Allow-overrides-Disallow correctness without checking.
  Per-host politeness **is** built (`ROADMAP.md` #9, resolved):
  `_parse_rules()` collects `Crawl-delay`, and `crawl_worker` passes it to
  `crawl/politeness.py::HostPoliteness.hold()`, which caps concurrent
  requests per host (`MAX_CONCURRENT_REQUESTS_PER_HOST`) and spaces
  requests by the site's `Crawl-delay` when given, else
  `DEFAULT_POLITENESS_DELAY_SECONDS`. Covered offline
  (`tests/test_politeness.py`, `tests/test_robots_cache*.py`). Neither
  docs.manim.community nor fastapi.tiangolo.com specifies a
  `Crawl-delay` (checked 2026-11-20), so a real site's value hasn't been
  exercised live. The defaults alone were too fast for
  docs.manim.community (`ROADMAP.md` #52), and a site-side 429 is retried
  with no backoff.
- **Progress display** (`progress_display.py`) is read-only against the
  frontier — `counts_by_status()`/`in_progress_urls()` only ever `SELECT`.
  Safe to poll on any interval; it cannot perturb the state machine.

## Working conventions in this repo

- No drive-by refactors — anything noticed outside the current task goes
  into `ROADMAP.md`, not into the diff.
- Ask before installing/upgrading a package, and before any crawl or
  LLM-billed run.
- Config values (concurrency, depth/page caps, chunk size/overlap,
  relevance thresholds, politeness delay) belong in config, not hardcoded
  in the module that uses them.
- Append to `LESSONS_LEARNED.md` after every significant session or run:
  what happened, root cause, fix, why it matters. Don't skip this even
  when nothing broke — confirmed non-obvious decisions belong there too.
- **Never kill processes by a broad filter (name/command-line pattern
  match) — always by a specific PID you already confirmed you started.**
  A `Where-Object { $_.CommandLine -like '*server.py*' }`-style filter
  matches every process fitting that shape, not just the one you meant,
  and it's exactly this kind of match that once took down `.venv` via
  `uv sync --active` (see the `uv` workflow section above) and, on a
  later occasion, killed 8 processes instead of 1 during cleanup —
  including some that had been running for hours before that session
  ever started. Both incidents share the same shape: a command scoped by
  "what it looks like" instead of "what I specifically launched," and
  both times the damage landed outside the task, not inside it. Capture
  the exact PID(s) at launch time and kill only those.
