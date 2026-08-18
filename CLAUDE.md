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
| `crawl/` | `frontier.py` | SQLite-backed crawl state machine, quiescence, retries |
| | `pipeline.py` | `crawl_worker`/`extract_worker`/`writer_worker` — the real loops `main.py` runs |
| | `robots_cache.py` | robots.txt/sitemap.xml/llms.txt, own RFC 9309 Allow/Disallow resolver |
| | `scope.py` | `normalize_url`/`derive_prefix`/`is_in_scope` — pure, no site-specific logic |
| | `discovery.py` | `discover_branches`/`fetch_page_links` — one-page link discovery |
| | `score_report.py` | `--score-report`: relevance-score distribution from a prior run |
| `content/` | `chrome_strip.py` | Nav/header/footer/aside/button exclusion, applied in `main.py::_make_fetch_fn` |
| | `extraction.py` | `parse_qa_json`, the Q&A system prompt |
| | `extraction_units.py` | Extraction-strategy chunk selection (`first_n_chars`/`per_chunk`/`top_k_chunks_by_relevance`) |
| | `sectioning.py` | `derive_section` (canonical record field) + filename slugify/cap/disambiguate (export time) |
| | `relevance.py` | Embedding-based relevance scoring (`score_headings`, chosen by measurement) |
| `storage/` | `writer.py` | Sole owner of the canonical JSONL file + Chroma upsert calls |
| | `chunk_store.py` | Content-hash chunk IDs, `ChunkStore.add_or_merge_chunk`, embedding-identity check |
| | `canonical.py` | The canonical record schema + `build_canonical_record` |
| | `query.py` | `--query`: embed a question, search Chroma, print parent text |
| `export/` | `export.py` | `-m export.export`: canonical.jsonl → training-format files (separate CLI) |
| | `export_formats.py` | Pure schema projections + validate/dedup/split (no I/O) |
| | `dataset_report.py` | `--dataset-report`: redundancy/duplicate diagnostics over a completed run |
| (root) | `main.py` | Interactive crawl CLI + `--dry-run`/`--score-report`/`--query`/`--dataset-report` |
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
text only — does **not** catch the near-duplicates `ROADMAP.md` #23
describes) → split (`split_records`, grouped by section/source_url,
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
  small pool, a few 429s idle the whole stage.
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
  Per-host rate limiting/politeness delay is **not** built yet —
  `Crawl-delay` isn't even collected by `_parse_rules()` (see
  `ROADMAP.md` #9); none of the 5 fixture sites specify one, so this is
  unverified against a real case, not proven safe.
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
