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

Ollama **cloud** is the opposite case — it's a stable HTTPS endpoint, so
routing it through `ChatOpenAI` (like the other hosted providers) is fine.
Local is the stated exception to the general provider pattern, not a
template to copy from.

## Queue architecture invariants

*(Filled in once the frontier/worker-pool crawler rebuild lands — see the
in-progress plan. Placeholder until then: do not reintroduce recursive
task-per-page spawning; crawling must stay a bounded work-queue.)*

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
