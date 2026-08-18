# CLAUDE.md

Guidance for working in this repo. Keep this current — update it whenever
the setup, workflow, or architecture invariants below change, not just when
someone remembers to.

## What this is

A prompt-steered crawler: give it a root URL and a natural-language intent,
it crawls, judges pages against that intent, and emits Q&A JSONL for
fine-tuning and/or a Chroma vector index. See `ARCHITECTURE.md` for the
as-built system and `ROADMAP.md` for known gaps. `LESSONS_LEARNED.md` is the
running problem → root cause → fix log — **append to it after every
significant step or run**, not just when something breaks.

Currently mid-rebuild per a step-by-step plan (steps tracked outside this
file, in-session). The pre-rebuild source is preserved in git history —
see the "Snapshot pre-rebuild source as-is" commit — before assuming
something described in `ARCHITECTURE.md` is still accurate, check whether a
later commit has already changed it.

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
uv run python main.py
```

Interactive prompts: root URL, branch/scope selection, LLM provider +
model, output formats (JSONL / Chroma), concurrency. (Exact prompts are
changing as the rebuild proceeds — see `ARCHITECTURE.md` for current
behavior.)

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
loaded env var before suspecting the endpoint — that's what actually
went wrong the first time (a corrupted `.env` line shadowing the real
key on case-insensitive Windows env vars), not the URL. Local Ollama
chat has no such doubt either way — only cloud's OpenAI-compat path was
ever in question. Local embeddings staying off the
OpenAI-compat wrapper remains the one hard, confirmed invariant above.

## Queue architecture invariants

Crawling is a bounded work-queue pipeline (`frontier.py` + crawl workers +
`content_queue` + extract workers + `results_queue` + one writer task), never
recursive task-per-page spawning. Full state machine and schema live in
`frontier.py`'s module docstring — this section covers the invariants that
matter when touching it.

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
deadlock. `put_content`/`put_results` in `frontier.py` release the lock
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

## Scope predicate (`scope.py`)

`normalize_url()`, `derive_prefix()`, and `is_in_scope()` are pure
functions, no I/O — `pipeline.py`'s `crawl_worker` and `main.py --dry-run`
both call them, and they're unit-tested offline against real fixture data
from 5 structurally different sites in `tests/fixtures/` (see
`tests/fetch_fixtures.py` to regenerate). **No site-specific logic
belongs in `scope.py`** — if a site needs different behavior, it has to
come from config (host allowlist, prefix list, exclude patterns), never a
branch on a domain name inside the predicate. `derive_prefix` has already
had one real bug from over-fitting to a single test site (a universal
`dirname()`-before-`commonpath()` heuristic that broke on any branch
containing its own section-index page — see `LESSONS_LEARNED.md` #8);
treat that as the standing warning it is before changing this file.

## Worker loops (`pipeline.py`, `writer.py`, `extraction.py`, `robots_cache.py`, `progress_display.py`)

`crawl_worker` / `extract_worker` / `writer_worker` in `pipeline.py` are
the real implementations of `frontier.py`'s design — built and tested
against stubs (`tests/stub_fetcher.py`, inline stub `score_fn`/`extract_fn`
in the test files) before any of them ever touched the network. Not wired
into `main.py` yet; `orchestrator.py` (the old single-queue crawler) is
still what actually runs.

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
- **Malformed LLM JSON**: `extraction.py::parse_qa_json` tries direct
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
- **`Writer` (in `writer.py`) has exactly one caller: `writer_worker`.**
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
  anyway. Per-host rate limiting/politeness delay is **not** built yet —
  `Crawl-delay` is parsed by stdlib `RobotFileParser` but never read (see
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
