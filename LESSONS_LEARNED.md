# Lessons Learned

Living document. Append after every significant session or run — problem →
root cause → fix → why it matters. Don't edit past entries except to correct
factual errors; add new ones at the bottom.

---

## 2026-08-18 (seeded from prior-owner history, unverified by this session)

### 1. NumPy 2.x broke PyTorch/LangChain
- **Problem**: Installing the project's dependencies pulled NumPy 2.x, which
  broke PyTorch and LangChain at import/runtime.
- **Root cause**: NumPy 2.0 has ABI/API breaking changes; several ML-adjacent
  packages hadn't caught up at the time.
- **Fix**: Pin `numpy<2` in an isolated `uv` venv (Python 3.12).
- **Why it matters**: `.venv` currently has `numpy==1.26.4` — the fix is real
  and in effect. But it is **not recorded in any tracked file**: `pyproject.toml`
  has no `[project]`/dependencies section, `requirements.txt` has no numpy pin
  at all, and `uv.lock` is effectively empty (3 lines, no resolved packages).
  If `.venv` is ever deleted and rebuilt from what's tracked, this fix is lost
  silently. See `ROADMAP.md` — this is now a blocking/fragile item, not a
  closed one.

### 2. LangChain package split (text splitters / stores / retrievers)
- **Problem**: Modern LangChain moved text splitters, vector stores, and
  retrievers into separate packages; old imports broke.
- **Root cause**: Upstream package restructuring.
- **Fix**: Updated imports to `langchain_text_splitters`, `langchain_chroma`,
  etc., and hand-wrote Parent-Child chunk storage instead of pulling in
  `langchain-classic`'s retriever abstractions.
- **Why it matters**: Confirmed current — `output_manager.py` imports from
  `langchain_text_splitters` and `langchain_chroma` directly, and the
  Parent-Child logic in `_add_to_rag` is fully custom (no
  `ParentDocumentRetriever`, no `InMemoryStore`). This does mean there is
  currently **no retrieval code at all** — the custom approach solved the
  storage-side breakage but retrieval was never built on top of it. That's a
  separate gap, not a regression of this fix.

### 3. LangChain's OpenAI wrapper routed local Ollama calls to random dynamic ports
- **Problem**: Using `langchain_openai`'s embeddings wrapper against a local
  Ollama endpoint sent requests to unpredictable ports instead of
  `localhost:11434`.
- **Root cause**: Wrapper-level transport/proxy behavior in the OpenAI SDK
  that `langchain_openai` inherits.
- **Fix**: Hand-wrote `LocalOllamaEmbeddings` in `llm_factory.py` — a plain
  `requests.post` to `http://localhost:11434/api/embeddings`, bypassing the
  OpenAI SDK transport entirely.
- **Why it matters**: Confirmed current and in use — `output_manager.py`'s
  Chroma client is constructed with this class as `embedding_function`. Note
  the fix only covers *embeddings*; the *chat* model (`ChatOpenAI` in
  `llm_factory.py:40-44`) still goes through the OpenAI SDK wrapper for all
  three providers, including Ollama-as-chat. If the same port issue ever
  applies to chat completions (not just embeddings), it hasn't been
  addressed — worth watching if Ollama chat calls behave oddly.

### 4. Early scrapes produced repetitive nav/sidebar/TOC questions
- **Problem**: The LLM's Q&A extraction generated a lot of low-value
  questions about page chrome (nav menus, sidebars, tables of contents)
  instead of real technical content.
- **Root cause**: Generic extraction prompt with no instruction to ignore
  boilerplate; markdown from crawl4ai includes nav/sidebar text inline with
  page content.
- **Fix**: Rewrote the system prompt (`output_manager.py:44-52`) to
  explicitly instruct the model to ignore nav/menu/footer content, focus on
  code/API/commands/concepts, and return an empty list if a page has no real
  content.
- **Why it matters — this fix does not appear to be working.** The data
  currently on disk (`data/en.jsonl`, `data/unified.jsonl`, crawled from
  `docs.manim.community`) is almost entirely questions *about the navigation
  menu itself* — "What main sections are listed in the navigation menu...",
  "What installation methods are documented in the... navigation menu?",
  etc. This is exactly the failure mode the prompt rewrite was supposed to
  prevent. Two possible explanations, unverified: (a) the prompt isn't
  actually preventing it and needs a harder rule or few-shot examples, or
  (b) this particular site's pages are themselves nav-heavy with little
  unique body content per page, and a nav-heavy page correctly produces
  nav-related-but-still-real Q&A under the letter of the prompt. Needs a
  fresh test run against a code-heavy page (e.g., an API reference page with
  real parameter tables) before concluding either way. **Do not assume this
  is fixed.**
- **Amendment (step 4, 2026-08-18) — "nav junk" was conflating three
  distinct causes, and the diagnosis was only ~40% right.** Dumping and
  categorizing all 87 duplicate Chroma chunks from the original manim
  crawl (not guessed — every entry read) found:
  - **23 pairs / 76 rows**: inline UI chrome — "Copy to clipboard" /
    "Make interactive" button text that crawl4ai's markdown conversion
    bakes into every code block. Not nav/sidebar at all; lives *inside*
    content blocks, so semantic-tag stripping (`nav`/`aside`/`footer`)
    can't touch it.
  - **14 pairs / 28 rows**: true sidebar/TOC chrome (changelog version
    listings, link lists) — this is what the original diagnosis actually
    described, and it's real, but it's less than a sixth of the total.
  - **49 pairs / 125 rows — the majority — is not chrome at all.** Real
    documentation content duplicating for two different reasons: the same
    example code shown twice *within* one page (gallery preview + full
    source), and genuinely identical instructional text repeated *across*
    pages by the site's own authors (the same "install the package"
    paragraph copy-pasted onto the Linux/macOS/Windows/uv install pages).
    No parser-layer fix should touch this — it's correct content sitting
    on legitimate, distinct pages.
  - **Why this matters going forward**: the JSONL nav-menu-question
    problem this entry describes may have more than one root cause too,
    not just "the prompt isn't working." A page that's genuinely dominated
    by inline UI chrome and repeated boilerplate has less real unique
    content for the LLM to extract from *regardless* of how well the
    prompt filters navigation — so before concluding the prompt fix failed
    outright, rule out how much of the page's actual text was chrome/
    boilerplate to begin with. Don't re-diagnose "nav junk" as one thing
    again; it's at minimum three: inline code-block UI, sidebar chrome,
    and genuinely repeated source content, each needing a different fix
    (a code-block UI filter, semantic-tag stripping, and content-level
    dedup, respectively — see ROADMAP.md and this file's step-4 entry).

---

## 2026-08-18 — Step 0/1 of the rebuild: repo hygiene

### 5. `.env` held a live plaintext secret with no `.gitignore` to catch it
- **Problem**: `.env` contained a commented-out but intact NVIDIA API key
  and a dead `ollama=` token nothing reads. No git repo existed yet, so
  nothing had leaked via history — but nothing was stopping a careless
  `git add .` from baking it in on the first commit.
- **Root cause**: repo hygiene (`.gitignore`, `git init`) was never set up
  before secrets were dropped into a plaintext file at the project root.
- **Fix**: `.gitignore` (excluding `.env`, `.venv/`, `chroma_db/`, `data/`,
  `__pycache__/`) was committed *before* any other file was ever staged.
  Every commit since has staged files by explicit name, never `git add -A`
  or `git add .`, as a second layer of protection during the window before
  `.gitignore` existed. `.env` itself was then edited locally to drop the
  dead `ollama=` line and blank the NVIDIA key; `.env.example` documents
  the two vars `config.py` actually reads (`OPENAI_API_KEY`,
  `NVIDIA_API_KEY`), names only.
- **Why it matters**: the NVIDIA key still needs rotating on NVIDIA's
  console — it sat in cleartext on disk before this, and stripping it from
  the tracked file doesn't undo that exposure. Not done yet; flagging so
  it isn't forgotten. Also: git init ended up happening in step 0 (to make
  that step's "commit the freeze" possible) rather than step 1 as
  originally planned — safe only because nothing but the freeze file was
  ever staged before `.gitignore` landed. Future steps should not assume
  the step numbers in the plan map 1:1 to commit ordering; check `git log`
  for what's actually landed.

---

## 2026-08-18 — Step 2 of the rebuild: dependency manifest + verification

### 6. `uv sync` meant for a scratch venv modified the live `.venv` instead
- **Problem**: Verifying the new `pyproject.toml`/`uv.lock` was supposed to
  happen in an isolated scratch venv (`../scraper-verify-venv`), created
  specifically so the live working environment wouldn't be touched. The
  command used to sync into it —
  `uv sync --python ../scraper-verify-venv/Scripts/python.exe --active` —
  instead modified this project's own live `.venv`: it uninstalled
  `langchain`, `langgraph`, `langgraph-checkpoint`, `langgraph-prebuilt`,
  `langgraph-sdk`, and `ormsgpack`, and changed `tiktoken`/`websockets` to
  different versions. The scratch venv itself was never touched — it had
  zero packages installed the whole time.
- **Root cause**: two flags were combined under a wrong assumption about
  what they do. `--python <path>` selects which *interpreter* uv resolves
  and installs against (for version-compatibility purposes) — it does
  **not** redirect *where* `uv sync` installs packages. `--active` tells
  `uv sync` to prefer whatever venv `$VIRTUAL_ENV` points at; with nothing
  activated in this shell, it silently fell back to `uv sync`'s ordinary
  default target — the project's own `.venv` in the current directory.
  That's the exact environment the check was supposed to leave alone.
- **Fix**: two parts. (1) *Recovery*: `uv pip sync requirements.lock.txt
  --python .venv/Scripts/python.exe` reinstalled the six removed packages
  and reverted `tiktoken`/`websockets`, restoring the live `.venv` to
  exactly what `requirements.lock.txt` recorded — proven, not assumed, by
  re-running `uv pip freeze` and diffing it against `requirements.lock.txt`
  (empty diff). (2) *Redo, structurally*: instead of any flag or env var
  meant to redirect `uv sync`'s target, the repo's source + `pyproject.toml`
  + `uv.lock` were copied (no `.venv`) into a directory with no project of
  its own (`D:\scraper-sync-check`, outside `D:\scraper`). With no project
  `.venv` present to fall back to, this failure mode can't recur — there's
  nothing for the fallback to fall back *to*. Verified with `uv sync
  --dry-run` printing the resolved target path before the real sync ran,
  then confirmed the six pipeline modules import successfully from that
  isolated copy using its venv's interpreter by full path, with each
  module's `__file__` printed to prove it resolved from the repo copy (not
  an installed package) — and diffed that copy's source files against the
  live repo's to confirm they were byte-identical to begin with.
- **Why it matters**: the verification step mutated the very environment
  it was meant to verify — exactly the failure mode a "prove it, don't
  assume it" habit exists to catch, and it very nearly went unnoticed
  since the sync command reported success. It was recoverable *only*
  because step 0 froze the environment (`requirements.lock.txt`) before
  any other rebuild work started — without that snapshot there would have
  been no ground truth to restore to, and the exact working combination
  that fixed `LESSONS_LEARNED.md` #1–#3 could have been lost silently.
  Going forward, any `uv` command that can install or uninstall gets a
  `--dry-run` first, with the resolved target path read and confirmed,
  before it runs for real (now in `CLAUDE.md`).
- **Verification scope, stated explicitly**: `uv.lock` resolves
  dependencies cross-platform, but the import check above only ran on
  Windows (both the live `.venv` and the isolated copy). `uvloop` and any
  other transitive dependency gated to non-Windows platform markers remain
  **unverified** until this manifest is synced and import-checked on
  Linux. Not claiming full verification here — only what was actually
  proven on this platform.

---

## 2026-08-18 — Step 2 close-out: Ollama cloud provider wiring

### 7. Ollama Cloud has no embedding models; its OpenAI-compat chat endpoint is unconfirmed by primary docs
- **Problem**: The rebuild plan called for adding Ollama cloud as a 4th
  provider, "selectable for both extraction and embeddings," on the
  assumption (mine, in the original plan) that routing it through
  `ChatOpenAI` like the other hosted providers would just work, mirroring
  local Ollama's `http://localhost:11434/v1` OpenAI-compat layer.
- **Root cause / what was actually verified**: fetched docs.ollama.com
  directly (authentication, cloud, capabilities/embeddings,
  api/openai-compatibility pages) plus the live cloud model catalog at
  ollama.com/search?c=cloud, rather than inferring from the wrapper's
  expectations.
  - **Embeddings: confirmed absent.** The cloud model catalog lists 16
    models (glm-5.2, deepseek-v4-flash, kimi-k3, gemma4, glm-5.1,
    minimax-m2.7, nemotron-3-super, minimax-m3, kimi-k2.7-code, kimi-k2.6,
    deepseek-v4-pro, nemotron-3-ultra, qwen3.5, nemotron-3-nano,
    mistral-large-3, gpt-oss) — every one is chat/vision/tools/thinking.
    Zero are tagged embedding. The dedicated embeddings docs page only
    ever shows `http://localhost:11434/api/embed`. There is no cloud
    embeddings endpoint to route to, full stop.
  - **Chat: base_url is a documented gap, not a confirmed fact.**
    docs.ollama.com's own programmatic-access examples for cloud (Python
    client, JS client, curl) all use `https://ollama.com` as host talking
    the **native** Ollama protocol (`/api/chat`), with
    `Authorization: Bearer $OLLAMA_API_KEY` — never an OpenAI-compatible
    `/v1/chat/completions` path. The dedicated openai-compatibility docs
    page documents only `http://localhost:11434/v1` and never mentions
    ollama.com at all. `https://ollama.com/v1` (what got wired into
    `config.py`) is reported by secondary/aggregator sources, not by any
    primary Ollama documentation page — it's a plausible mirror of the
    local pattern, not a confirmed one.
- **Fix**: `config.py` gained `LLMProvider.OLLAMA_CLOUD` and an
  `EMBEDDING_CAPABLE_PROVIDERS` set that excludes it, with the base_url
  uncertainty spelled out in a comment at the exact point someone would
  need to know it. `llm_factory.py`'s `get_llm()` embeddings line is
  unconditional (`LocalOllamaEmbeddings` regardless of chat provider) —
  already structurally incapable of routing embeddings to
  `OLLAMA_CLOUD`'s base_url/key, since that code path never reads
  `config` at all. No live call was made against `https://ollama.com/v1`
  — no `OLLAMA_API_KEY` was available this session, and this step's rule
  was explicitly not to spend a billed call just to verify wiring.
  `.env.example` uses `OLLAMA_API_KEY` (Ollama's own documented variable
  name, matching what `ollama signin` and their CLI already use) rather
  than a project-invented name.
- **Why it matters**: this is wired but **unverified in two separate,
  independent ways** — not just "untested because no API key" (which
  would be a normal, benign gap) but "the exact endpoint may not exist as
  configured" (a real, non-benign gap). Both are stated explicitly here
  and in `config.py` rather than left implicit, per the same discipline
  applied to the cross-platform verification gap in #6. **Before this
  provider is used for anything real**: get an `OLLAMA_API_KEY` and make
  one cheap test call; if `https://ollama.com/v1/chat/completions` 404s or
  errors, the fallback is Ollama's confirmed-working native protocol
  (`https://ollama.com/api/chat`), which needs a different client than
  `ChatOpenAI` — likely the same shape of fix as `LocalOllamaEmbeddings`
  was for local embeddings (a small native HTTP class), not another
  guessed OpenAI-compat path. Embeddings for Ollama cloud are not a "not
  implemented yet" gap — they're "does not exist upstream"; don't build
  toward them later without re-checking Ollama's model catalog first.

---

## 2026-08-18 — Step 3: scope predicate rebuilt, tested against 5 real sites

### 8. `derive_prefix`'s own fix broke on the second real site it was tested against
- **Problem**: `derive_prefix` (`scope.py`) replaces the original bug
  (branch scoping matched exact leaf URLs from the root page instead of a
  path prefix). The first version took the *dirname* of every URL in a
  branch before computing their longest common path, on the theory that
  this would stop a single-URL branch from deriving an exact-leaf-file
  prefix nothing else could ever match. It passed all 27 unit tests and
  looked correct against `docs.manim.community` (dry-run step 3 kickoff).
  Tested against `fastapi.tiangolo.com` as one of five required
  structurally-different sites, **every single path-based branch came
  back degenerate** — including a 51-URL `/tutorial/*` branch that should
  obviously have scoped to `/tutorial/`.
- **Root cause**: FastAPI's tutorial section has both an index page
  (`/tutorial`) and its subpages (`/tutorial/body`, `/tutorial/security`,
  ...) in the same branch — an extremely common docs-site pattern.
  `dirname("/tutorial")` = `/` (it treats "tutorial" as a filename inside
  the root, not as the section's own name), and `commonpath()` across a
  set that includes `/` collapses the *entire branch* to the site root,
  triggering the degenerate host-only fallback for a branch that
  obviously shared a real, meaningful prefix. The dirname-based heuristic
  was written to solve a rare case (single-URL branches) but applied
  *universally*, breaking the common case (a section's index page sitting
  alongside its own subpages) instead. `docs.manim.community`'s branches
  never happened to include a bare section-index URL, so the first site
  tested against couldn't have caught this — exactly why testing against
  one friendly site isn't a real test of a general-purpose predicate.
- **A second, related bug surfaced by fixing the first**: once
  `derive_prefix` correctly returned `/tutorial/` (with a trailing slash)
  for that branch, `is_in_scope`'s own `/tutorial` URL (the section's bare
  index page, itself part of the branch the prefix was derived from) got
  *rejected* by its own prefix — `"/tutorial".startswith("/tutorial/")`
  is `False` in Python (the bare path is shorter than the slash-terminated
  prefix). Fixed by comparing `(path + "/").startswith(prefix)` instead of
  `path.startswith(prefix)`, which accepts the bare section URL while
  still correctly rejecting a merely text-prefixed sibling like
  `/tutorial2` or `/how-toz`.
- **Fix**: `derive_prefix` now only applies the dirname heuristic when a
  branch has exactly one *distinct* path (no sibling data to disambiguate
  "this URL is a directory" vs. "this URL is a leaf file"). For any branch
  with more than one distinct path, it computes `commonpath()` on the raw
  paths directly — an index page is already a valid prefix of its own
  subpages, so `commonpath` finds the right boundary without any
  adjustment needed. Both bugs got dedicated regression tests
  (`tests/test_scope.py`): a 3-URL section-index-plus-subpages case for
  `derive_prefix`, and a bare-index-URL-matches-its-own-prefix /
  sibling-still-rejected pair for `is_in_scope`.
- **Why it matters**: this is exactly the failure mode multi-site testing
  exists to catch, and exactly why the correction to test against 5
  structurally different sites (not just the one the original broken
  artifacts came from) mattered — a scope predicate that only sees one
  site's URL shapes will silently overfit to it. Two other outcomes from
  the same testing round, reported honestly rather than forced: the
  chosen "JS-rendered nav" test site (stackblitz.com) came back
  `required_rendering=False` (73% href overlap between server- and
  browser-rendered versions) — it didn't actually demonstrate the
  JS-required case, its nav chrome turned out to be present in static
  HTML. And none of the 5 sites' root pages exposed live `?page=N`-style
  pagination links, so the "pagination is a scoping trap" case (real,
  and `is_in_scope` genuinely doesn't filter it — see `ROADMAP.md` #15a)
  stayed undemonstrated against real fixture data this round, not because
  it isn't real but because the picked site's root page didn't happen to
  surface it.

---

## 2026-08-18 — Step 4: chrome-stripping built and tested offline against 5 sites

### 9. Parser-layer chrome stripping, developed and measured against cached fixtures
- **What was built** (`chrome_strip.py`): two layers, no site-specific
  logic. (1) Structural — `nav`/`header`/`footer`/`aside`/`button`/
  `script`/`style`/`noscript` tags plus ARIA-role selectors
  (`role="navigation"`, `role="banner"`, `role="contentinfo"`,
  `role="complementary"`, `role="button"`, `aria-hidden="true"`) excluded
  via crawl4ai's own `excluded_tags`/`excluded_selector` HTML-cleaning
  stage (`LXMLWebScrapingStrategy`), same mechanism `CrawlerRunConfig`
  exposes for the real pipeline. (2) Text-pattern fallback — a small
  config-driven phrase list (`DEFAULT_TEXT_PATTERNS`:
  "copy to clipboard", "copied to clipboard", "copied!",
  "make interactive") for UI text that survives structural exclusion,
  matched as a whole line only (not substring-anywhere), so real content
  that happens to *discuss* clipboard copying in a sentence isn't deleted.
- **Verified `<button>` exclusion against a real, confirmed instance**:
  fastapi.tiangolo.com's cached HTML has
  `<button class="md-code__button" title="Copy to clipboard" ...>` on
  every code block — a genuine `<button>` tag, no theme-specific class
  needed to target it. Excluding the tag removed it.
- **Measured before/after across all 5 fixtures, offline** (no network):

  | site | chars before → after | survival | dup pairs before → after |
  |---|---|---|---|
  | docs.manim.community | 67,970 → 14,505 | 21% | 7 (all nav-pattern) → 0 |
  | fastapi.tiangolo.com | 47,456 → 24,647 | 52% | 2 (real content) → 3 (real content) |
  | www.manim.community | 8,365 → 5,708 | 68% | 0 → 0 |
  | stackblitz.com | 4,072 → 2,629 | 65% | 0 → 0 |
  | blog.cloudflare.com | 29,505 → 18,147 | 62% | 7 (6 nav + 1 real) → 0 |

  Every nav/link-list-pattern duplicate, on every site that had one,
  went to zero. Real-content duplicates (fastapi's) stayed flat (2→3,
  noise-level) — exactly the expected outcome per the corrected
  understanding in `LESSONS_LEARNED.md` #4's amendment: category C isn't
  a chrome problem, so stripping correctly leaves it alone rather than
  accidentally destroying real content. **Cross-page duplicate reduction
  was not measured** — each fixture is a single root page, so the
  cross-page category-C cases (installation steps repeated across
  Linux/macOS/Windows/uv pages) can't be exercised from this fixture set;
  they're expected to persist regardless of stripping (see `ROADMAP.md`
  #6's revised chunk-ID fix, which is the actual mechanism for that case,
  not chrome-stripping).
- **Honest gap, not papered over**: the `<button>` exclusion is verified
  against FastAPI's real markup, not manim's. None of the 5 cached
  fixtures are the manim pages that actually produced the original
  "Copy to clipboard" / "Make interactive" duplicate chunks (`examples.html`,
  `installation/*.html`, `tutorials/*.html` — the root page fixture used
  for manim throughout this project doesn't have code blocks). So for
  manim specifically, it's the **text-pattern fallback** doing the
  confirmed work, not structural exclusion — Furo's sphinx-copybutton
  markup may render differently than mkdocs-material's `<button>`. Worth
  fetching one of those specific manim pages before considering the
  manim case structurally (not just textually) solved.
- **Also confirmed empirically**: `chunk_overlap` defaults to 200 on both
  the 2000-char parent and 400-char child `RecursiveCharacterTextSplitter`
  in `langchain_text_splitters==1.1.2` (the version actually installed) —
  50% overlap on child chunks, documented cause rather than inferred, for
  the 9,204-vector figure in the original audit.

---

## 2026-08-18 — Step 4 close-out: two-page extraction test

### 10. `.env` was still corrupted from the earlier paste mishap -- and it silently broke the Ollama cloud key
- **Problem**: The first real extraction call against Ollama cloud
  failed with `401 Unauthorized`, which looked at first like it might
  confirm the endpoint uncertainty flagged in `LESSONS_LEARNED.md` #7
  (`https://ollama.com/v1` unconfirmed by primary docs).
- **Root cause**: it wasn't the endpoint. `.env` still had a leftover
  corrupted line from an earlier paste mishap:
  `ollama_api_key=# Copy this file to .env and fill in real values...`
  (the entire contents of `.env.example`, mashed onto one line, under a
  lowercase key name). Windows environment variables are case-insensitive,
  so `ollama_api_key` and `OLLAMA_API_KEY` are the same underlying slot;
  `python-dotenv`'s default `override=False` meant the garbage value set
  first (from the lowercase line) blocked the correct value on the later
  `OLLAMA_API_KEY=...` line from ever taking effect.
  `os.getenv("OLLAMA_API_KEY")` was silently returning a truncated
  comment string as the "key" the whole time.
- **Fix**: deleted the corrupted line. Verified before/after by printing
  the loaded key's length and a truncated form -- garbage string first,
  real 57-character key after.
- **Why it matters**: a `401` reads exactly like "your credential is
  wrong," and it's tempting to jump straight to "the endpoint config
  must be wrong" (the *documented* uncertainty) rather than "check what's
  actually in the env var" (the *undocumented* one). Verify the simpler,
  more mechanical explanation before the more interesting architectural
  one. Also: this correctly resolves as **`https://ollama.com/v1` does
  work** with a real key and `deepseek-v4-flash` -- the step 2 endpoint
  uncertainty in `LESSONS_LEARNED.md` #7 is no longer "untested," it's
  confirmed working for chat/extraction. (Embeddings remain confirmed
  absent from Ollama cloud regardless -- unrelated finding, unchanged.)

### 11. Chrome-stripped extraction test: real, usable Q&A on both pages
- Ran the actual production code path (`OutputManager._generate_qa`,
  unmodified, same system prompt and 4000-char truncation) against
  chrome-stripped markdown for two real pages, via Ollama cloud /
  `deepseek-v4-flash`:
  - **Page 1** (`docs.manim.community` root -- the exact nav-heavy
    landing page whose *unstripped* Q&A output was the original evidence
    for `LESSONS_LEARNED.md` #4's "nav junk" problem): 5 pairs, all
    genuine FAQ-style questions about Manim itself (what ManimCE is and
    how it differs from the original, where to find install instructions,
    how to try it without installing, where tutorials/help live). **Zero**
    questions about the navigation menu's own structure -- the exact
    failure mode #4 documented is gone on this page once chrome is
    stripped before extraction.
  - **Page 2** (`manim.mobject.geometry.arc.Circle` reference page): 5
    pairs, all real API content -- constructor parameters, a usage
    example, the methods list, inherited attributes, a full code sample
    with explanation. This is exactly the kind of content the original
    system prompt asked for and the unstripped pipeline wasn't reliably
    producing.
  - Fixture used for page 2: `tests/fixtures/docs_manim_reference.html`
    (added this step) -- also the fixture that confirmed `<button>`
    exclusion structurally removes manim's actual "Copy to clipboard" /
    "Make interactive" markup (Furo's `<button class="copybtn"
    data-tooltip="Copy">` and `<button class="manim-binder-button">Make
    interactive</button>` -- both genuine `<button>` tags, closing the
    "honest gap" noted in entry #9).
- **Why it matters**: this is the actual verification step 4 was for --
  the chunk-dump categorization (#9) explained *what* was duplicating and
  *why*, but only a real extraction call proves whether the fix produces
  usable training data. It does, on both a chrome-heavy landing page and
  a dense reference page, which were chosen specifically to be different
  page shapes rather than both being favorable cases. Two real,
  unmodified pairs of evidence beat one plausible-sounding theory.

---

## 2026-08-18 — Step 5 (part 1): frontier.py, quiescence design, and a real termination bug caught before it shipped

### 12. Per-stage sentinel shutdown was wrong the moment extraction started feeding back into the frontier
- **Problem**: the original plan's shutdown design (frontier drains →
  crawl workers get sentinels and exit → content queue drains → extract
  workers get sentinels → results queue drains → writer exits) assumed
  each stage's completion only depends on the stage before it. It
  doesn't — an extract worker promotes `pending_score → queued`, which
  means the crawl stage's "am I done" question depends on the *extract*
  stage, not just its own queue. "Crawl stage idle, nothing queued" can
  be true while a slow extract worker is mid-flight and about to
  produce an entire new wave of claimable work. A cascade design reads
  that idle moment as "crawl is done," dispatches crawl-worker
  sentinels, and the run ends after roughly one wave — silently, with
  no error, just an incomplete crawl that looks like it finished
  cleanly.
- **Root cause**: the pipeline was drawn as a linear A→B→C chain when
  designing shutdown, but it's actually cyclic — extraction feeds back
  into the frontier that crawling reads from. A linear shutdown protocol
  is only correct for a linear pipeline.
- **Fix**: replaced per-stage sentinel cascades with one global
  quiescence check: a single `in_flight` counter (incremented on claim,
  `content_queue.put`, `results_queue.put`; decremented on leaving
  `in_progress`, and on each queue's `task_done`), checked under the
  same lock that mutates it. The run ends only when `in_flight == 0`
  **and** (zero `queued` rows remain **or** `max_pages` is reached) —
  verified with a literal `SELECT COUNT(*) WHERE status IN
  (queued,pending_score,in_progress)` assertion whenever that condition
  fires, so a violation of the invariant fails loudly instead of
  quietly shipping a truncated crawl again.
- **Caught before any worker code existed**: `tests/test_frontier_quiescence.py`
  drives `frontier.py`'s real API with a deliberately slow extract
  worker against a fast crawl worker over an 8-node graph shaped so the
  crawl stage must idle multiple times waiting for promotions (measured
  4 idle-then-resume cycles in one run, asserts ≥2). All 8 nodes reach
  `done`; a cascade design would have stopped after node C at the
  latest. Built and passing *before* `frontier.py`'s workers existed,
  per the explicit build order — the test was shaped by the design, not
  the other way around.
- **Why it matters**: this is exactly the kind of bug that "works" in
  every manual test with a fast, low-latency LLM and only shows up under
  real production latency (slow provider, rate limiting, a genuinely
  large page) — by which point it looks like "the crawl just didn't
  find much" rather than "the shutdown logic raced itself." Caught at
  design-review time, before a single worker existed to hide behind.

### 13. JSONL crash-resume duplicates: file-layer marker, not a same-transaction DB column
- **Problem**: `status='done'` is set by the writer, only after the
  JSONL append and Chroma upsert both succeed — correct, so a crash
  before persistence leaves the row retryable rather than falsely
  `done`. But that retry re-runs extraction and re-appends to JSONL. Once
  step 7's content-hash chunk IDs land, Chroma's upsert absorbs the
  duplicate; JSONL has no such mechanism and would get a real duplicate
  row.
- **Two options considered.** (a) Hash the Q&A content and dedup on
  load — rejected: LLM output isn't guaranteed byte-identical between
  the original attempt and the retry (different phrasing of the same
  answer is plausible), so an exact-content hash wouldn't reliably catch
  the duplicate. (b) A same-transaction SQLite "written" marker,
  updated alongside `status='done'` — rejected on inspection: the whole
  reason this bug exists is a crash *between* the file write and the
  status-update transaction committing. A marker inside that same
  transaction is exactly as uncommitted as the status update is when
  that crash happens — it can't detect a scenario it's part of.
- **Fix chosen**: extend the JSONL rows with a `source_url` field, and
  have the writer preload the set of already-written URLs from existing
  JSONL files at startup — the same pattern `_preload_existing_instructions()`
  already uses for exact-instruction dedup, just keyed by URL instead of
  instruction text. Before appending, check membership; if already
  present, skip the JSONL write (the content is genuinely already
  captured) but still let the row reach `done` normally. The file itself
  is the durable record of "was this persisted," independent of
  whatever the frontier's status column says — which is exactly what
  survives the crash window a DB-side marker can't.
- **Why it matters**: recorded now, per the ask, rather than discovering
  it as a mysterious duplicate-rows bug during step 8's kill test and
  re-deriving this same reasoning under pressure.

---

<!-- Append new entries below this line, most recent last, dated. -->
