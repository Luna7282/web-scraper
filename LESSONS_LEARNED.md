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

## 2026-08-18 — Step 5 (part 2): worker loops, tested against stubs before any network call

### 14. Testing a retry meant testing a re-crawl, not just a re-extraction
- **Problem**: writing failure-mode tests for `extract_worker` (malformed
  JSON, rate limits), the first attempt seeded `content_queue` with one
  static item and ran `extract_worker` alone. Every test that exercised
  an actual retry (not just the first failure) hung until timeout.
- **Root cause**: by design (`in_progress` spans crawl *and* extract,
  `LESSONS_LEARNED.md` #12), a requeued row goes back to `status='queued'`
  — getting new content requires being claimed and fetched again, not
  just re-popped from `content_queue`. `mark_extract_outcome`'s retry
  path only touches the frontier row; it doesn't and shouldn't know how
  to re-supply content. A test that only runs `extract_worker` has
  nothing to perform that re-fetch, so after the first failure the row
  sits in `queued` forever with nothing claiming it.
- **Fix**: added a trivial `recrawl_stub` helper to the test file — claims
  a queued row and immediately supplies its (fixed) content, standing in
  for a real `crawl_worker` without dragging in `scope_check`/`fetch_fn`
  concerns unrelated to what the test is actually checking. Two tests
  (crawl_worker's own success-path test, extract_worker's retry tests)
  had the same shape of bug for the same underlying reason and got the
  same fix.
- **Why it matters**: this is a direct, concrete consequence of the
  "single `in_progress` spans both sub-phases" design decision from step
  5 part 1 — both a quiescence-based integration test and a retry-path
  test need something to close the crawl→extract loop, not just drive
  one stage in isolation. Documented here so the
  next test added for a retry path doesn't rediscover this by watching a
  test hang for the first time.

### 15. robots.txt/sitemap.xml/llms.txt reality-checked against 5 real sites — two real gaps found, one reporting bug caught before it shipped bad fixtures
- **`Crawl-delay` is parsed by `RobotFileParser` but never read by
  `HostPolicy`.** None of the 5 fixture sites specify one (confirmed —
  not assumed — via `tests/fixtures/robots/*.json`), so this hasn't
  broken anything yet, but a real target site that does specify one would
  have it silently ignored. `crawl_worker` also has no per-host
  concurrency semaphore or delay mechanism at all yet — the original
  "no politeness" gap is only partially closed (robots.txt disallow is
  now respected; rate/concurrency throttling per host is not). See
  `ROADMAP.md` #9.
- **`blog.cloudflare.com/sitemap.xml` is a `<sitemapindex>`, not a flat
  `<urlset>`.** Confirmed by fetching and checking the root XML tag, not
  assumed — this is exactly the "large sites nest their sitemaps" case,
  and it showed up on the first real site checked that has a
  sitemap.xml. `robots_cache.py` records the URL but doesn't fetch or
  parse sitemap content at all yet, so nothing is broken by this today —
  but anything that later reads sitemap content needs to check the root
  tag and recurse one level, not assume `urlset`. See `ROADMAP.md` #9a.
- **The reporting script itself had a bug that would have saved garbage
  fixtures if not caught**: `requests` returns a response body on a 404
  (most sites serve a real HTML error page, not an empty body), and the
  first version of the fetch-and-save script only checked `if text:`
  before treating a response as "found" — meaning all five `llms.txt`
  404s (none of the 5 sites have one) got initially saved as if they were
  genuine 2,300–390,000-character `llms.txt` files, and `www.manim.community`'s
  404 sitemap page got saved as its `sitemap.xml`. Caught by checking the
  actual status codes in the saved JSON metadata (which *was* computed
  correctly — only the print statements and the file-save condition used
  the wrong check) before treating the fixture set as done, deleted the
  mislabeled files. `RobotsCache` itself was never affected — its own
  `fetch_text_fn` contract already checked `status_code == 200` correctly
  from the start; this was purely a bug in the one-off reporting/fixture
  script, not the production code.
- **Why it matters**: "including the misses" was the actual instruction,
  and a miss saved as if it were a hit is worse than not caching it at
  all — it would have taught `tests/test_robots_cache_fixtures.py` the
  wrong lesson permanently. Verify what a fetch script actually saved
  before trusting it as a fixture, the same discipline as verifying any
  other claimed-successful operation in this project.

### 16. Rate-limit backoff proven not to block a worker slot, not just designed that way
- `extract_worker`'s `RateLimitError` path calls
  `frontier.mark_extract_outcome(..., backoff_seconds=...)`, which sets
  `not_before` and releases the row back to `queued` rather than the
  worker sleeping in place (frontier.py's design from step 5 part 1).
  `tests/test_extract_worker.py`'s rate-limit test proves this rather
  than just asserting the code path was taken — it polls for the row to
  be back in `queued` (not `in_progress`) well before the requested
  backoff (5s) would have elapsed if the worker had actually slept, and
  fails loudly if that doesn't happen fast.

---

## 2026-08-18 — Step 6: relevance-gate scoring unit measured, not assumed

### 17. Wider score spread didn't mean better relevance signal -- headings beat both whole-page and max-chunk on the one thing that mattered
- **Problem being investigated**: a short intent string and a full page of
  markdown live at very different scales of specificity; cosine similarity
  between them was suspected to compress into a narrow, useless band.
  Measured before building anything, per the explicit ask, rather than
  assumed.
- **Method**: one intent ("How do I use the animation API to create and
  control geometric shapes?") embedded once, scored against all 6 stripped
  fixture pages (5 sites' root pages + the manim `Circle` API reference
  page added in step 4 -- the one page that's unambiguously, exactly what
  this intent describes) under three candidate strategies: whole-page
  embedding, headings-only embedding (falls back to whole-page if a page
  has no markdown headings), and max-similarity-over-chunks. 62 real
  embedding calls, `nomic-embed-text` via local Ollama, 768-dim, mean
  2.80s/call.
- **Result, not what was expected**: spreads were 0.12 (headings) to 0.28
  (whole-page) -- none of the three compressed into "a few hundredths," so
  the originally-suspected failure mode (scores too close together to set
  a threshold) didn't show up. A different, more important problem did:
  **`whole_page` and `max_chunk` both ranked a marketing page
  (`www.manim.community`, tagline-heavy, short) above the actual API
  reference page** for an intent specifically about the API. `headings`
  was the only strategy that ranked the genuinely correct page first --
  and it has the *smallest* spread of the three, and costs one embed call
  per page (same as whole-page, versus max-chunk's 5-13 calls/page for a
  worse answer).
- **Why it matters**: discrimination (how far apart the scores are) and
  correctness (whether the ranking matches genuine relevance) are not the
  same property, and it would have been easy to pick `whole_page` for
  having the widest, most "confident-looking" spread and ship a gate that
  systematically favors marketing copy over real documentation. The
  hypothesis was specifically "max-over-chunks is usually the right unit"
  -- measurement contradicted the default assumption, which is exactly why
  the instruction was to measure rather than assume it. **Caveat, stated
  explicitly**: this is one intent string against one known-correct page,
  not a multi-query validation -- treat `score_headings` as the reasoned
  choice from real data, not a proven-robust one. If it misranks on a
  different intent/site shape later, this entry is where to start.
- **Bug caught by this measurement, fixed before it mattered**:
  `score_headings`' no-headings fallback to `score_whole_page` passed
  *untruncated* content, and `score_whole_page` itself had a hardcoded
  `content[:8000]` truncation that 500'd against a real embedding server
  on real markdown (same root cause as `LESSONS_LEARNED.md` #10 --
  markdown tokenizes far less efficiently than plain prose, so a
  character-count truncation safe for one doesn't transfer to the other).
  `blog.cloudflare.com`'s stripped page has no markdown headings at all,
  hit the fallback, hit the bug, on the very first live run. Fixed with
  one shared `MAX_EMBED_CHARS = 4000` constant (same number already
  established as safe in `LESSONS_LEARNED.md` #10, and matching
  `output_manager.py`'s existing extraction-call truncation) applied
  everywhere `relevance.py` calls `embed_fn`, with regression tests for
  both the fallback path and an oversized `chunk_size` override.

---

## 2026-08-18 — Step 6 close-out: main.py wired to the new pipeline

### 18. Wiring main.py to the new pipeline incidentally fixed ROADMAP #1 in the path that actually runs
- Step 3 fixed the branch-scoping predicate itself (`scope.py`) but only
  ever wired it into `--dry-run` — the live crawl path (`orchestrator.py`,
  via the old `main.py`) still had the original bug the whole time
  (`allowed_branch_prefixes` set to literal leaf URLs, not a real prefix).
  Step 6's rewrite replaces `main.py`'s crawl invocation with the new
  pipeline (`_make_scope_check` built from `derive_prefix`/`is_in_scope`,
  feeding `crawl_worker` directly), so the corrected predicate now governs
  the code path that actually runs a crawl, not just the diagnostic tool.
  `orchestrator.py` still has the original bug — left alone, unreferenced,
  not deleted (step 9's job once the new path has proven itself on a real
  crawl). `ROADMAP.md` #1 marked resolved accordingly.
- **`main.py` no longer imports `orchestrator` or `output_manager` at
  all** — both stay fully importable (confirmed: `import orchestrator`
  still works standalone) but are referenced nowhere in the new flow,
  matching "build alongside, don't mutate in place, don't delete yet."
- **Provider menu gained a 4th option** (Ollama cloud) that the old
  `main.py` never had, even though `config.py`'s `LLMProvider` enum added
  it back in step 2 — the CLI menu had simply never been updated to match.
  Caught while rewriting this file for an unrelated reason (wiring the
  new pipeline), not as its own investigation — worth remembering that
  config-level provider additions don't automatically surface in the UI
  that's supposed to expose them.
- **Output scope deliberately narrowed for now**: the new `main.py` only
  offers unified JSONL output. `split_jsonl` and Chroma/RAG output
  (`build_rag`) existed as toggles in the old flow but aren't wired into
  `Writer`/the new pipeline yet — chunk IDs and Chroma upsert are step 7's
  job (`ROADMAP.md` #6). Stating this as a deliberate scope cut, not a
  silent regression: the old toggles still exist and still work via
  `orchestrator.py`/`output_manager.py` if needed in the meantime.
- **`--score-report` reads an existing frontier DB independently of any
  crawl** — pure DB read (`Frontier.all_scores()`) + pure formatting
  (`score_report.py`), tested end-to-end against a real temp-file
  `Frontier` instance (not a live crawl's output) to prove the two pieces
  actually connect, not just that each one works in isolation.

---

## 2026-08-18 — Step 7: content-hash chunk IDs, embedding-identity check, retrieval path

### 19. Chunk ID = hash of normalized text alone, verified to collapse the exact duplicate set the archive measured
- **Design**: `chunk_store.py::chunk_id()` hashes `normalize_chunk_text(text)`
  only — never `url + text` — so the same instructional text repeated
  across pages collides to one Chroma id instead of one row per page
  (`ROADMAP.md` #6's correction). `normalize_chunk_text` is defined
  strictly, not loosely: collapse all whitespace runs to a single space
  (`" ".join(text.split())`), lowercase, then strip trailing
  `.,;:!?`. Tested directly against two chunks differing only in
  whitespace (`tests/test_chunk_store.py`) — they collide.
- **Verified against the real archived pre-rebuild data, not just
  synthetic cases**: `tests/test_chunk_store_archived_data.py` reads
  `archive/pre-rebuild/chroma_db/chroma.sqlite3` directly and confirms
  every known cross-page duplicate group from the original audit dump
  (the "manimations" install-instructions text repeated across 4 real
  install pages, and the broader set of duplicate groups) collapses to a
  single `chunk_id` under the new function. This is the "assert the
  collapse, don't hope for it" bar from the ask, met against real data
  rather than a fixture built to make the test pass.
- **Sources recorded as a list, append-not-overwrite**:
  `ChunkStore.add_or_merge_chunk()` checks `collection.get(ids=[cid])`
  first; if the id already exists, it reads the existing `sources` list,
  appends the new URL only if not already present, and calls
  `collection.update()` (metadata-only, no re-embed) rather than
  `upsert()` — `upsert()` was empirically confirmed to replace metadata
  wholesale, not merge it, so using it here would have silently dropped
  earlier sources on every second write to the same chunk.

### 20. chromadb's write-path semantics were characterized empirically before ChunkStore was built around them
- Confirmed, not assumed, via `tests/test_chunk_store.py`: `add()` with a
  duplicate id **silently no-ops** and drops the new call's metadata
  entirely (no error, no update) — using `add()` for the merge path would
  have looked like it worked while quietly losing every second source.
  `upsert()` with a duplicate id **replaces metadata wholesale** — doesn't
  merge lists. `update()` changes metadata without needing to resupply an
  embedding, which is what makes the no-re-embed merge path possible.
  `get_or_create_collection()` preserves the *original* metadata on
  reopen — a second call with different `embedding_model`/`embedding_dim`
  arguments does not silently overwrite the first collection's recorded
  identity, which is exactly the property #21 below depends on.
  `EphemeralClient()` instances share collection state across the same
  process (not per-instance isolated) — this cost real test-debugging time
  (5 failing tests) before being traced to hardcoded collection names
  colliding across supposedly-independent test instances; fixed by
  generating a unique collection name per test.

### 21. Embedding-model identity is checked loudly on both the write and read path, and `collection.metadata` can be `None` entirely
- `get_or_create_collection()` writes `{"embedding_model": ..., "embedding_dim": ...}`
  into the collection's own metadata at creation. `verify_embedding_identity()`
  is called from both the write path (on every `get_or_create_collection`
  call, so a second run against an existing collection with a *different*
  model immediately raises `EmbeddingIdentityMismatch`, not a warning) and
  the read path (`query_chunks()`, so a query embedded with the wrong
  model fails loudly instead of returning confidently wrong
  nearest-neighbor results).
- **Bug caught during testing**: `collection.metadata` can be `None` in
  its entirety (not just missing the two expected keys) for a legacy
  collection created without a metadata argument — `metadata.get(...)`
  on `None` raises `AttributeError` before the mismatch check ever runs.
  Fixed with `metadata = collection.metadata or {}` before any `.get()`
  call. Caught by a test exercising a collection created via the plain
  `client.create_collection(name)` path with no metadata, not discovered
  in production.

### 22. Chunk sizes/overlap made explicit in config, decision to retune deferred
- `config.py` now states `PARENT_CHUNK_SIZE=2000`, `PARENT_CHUNK_OVERLAP=200`,
  `CHILD_CHUNK_SIZE=400`, `CHILD_CHUNK_OVERLAP=200` explicitly — the exact
  values `langchain_text_splitters` was defaulting to invisibly (confirmed
  in entry #9). Restating the entry #9 finding here since it's now a
  config value someone will actually look at: 200-char overlap on
  400-char children is 50% overlap, the documented cause of 9,204 vectors
  from 30 pages in the original audit. **Not changed in this commit** —
  making it visible and making it smaller are different decisions, and
  the ask was explicit that they shouldn't happen in the same commit.

### 23. The retrieval path (query.py) run for real, against a real cross-page collision
- `query_chunks()` embeds the question, searches child-chunk embeddings,
  and returns `parent_text`/`sources`/`child_text`/`distance` per match —
  this is the first code that reads from Chroma anywhere in the rebuild;
  everything before this step was write-only (`ROADMAP.md` #3).
- **Live demo, real embedding calls, local Ollama `nomic-embed-text`
  (768-dim)**: indexed 3 real fixtures (`circle_reference`, and two newly
  fetched pages, `docs.manim.community/installation/{linux,macos}.html`,
  fetched specifically because the cached fixture set had zero real
  cross-page duplication and a collision can't be demonstrated without
  one — kept permanently under `tests/fixtures/` per instruction, so this
  demo stays runnable offline).
  - Naive per-page vector count: 146 (38 + 60 + 60). Actual unique
    vectors after upsert: **100**. Collision reduction: **46**, all
    between the linux/macos pair (circle_reference shares nothing with
    either, as expected — it's a different content domain).
  - **This differs from the archive's original count (49 pairs) on
    purpose, not by error** — the archive measured duplication on
    *unstripped* content; chrome-stripping (step 4) changes what the
    chunker sees before chunk_id ever runs, so a changed count here is
    the expected interaction between two independently-built pieces, not
    a regression in either. 46 vs. 49 is close enough to read as "still
    working," not as a discrepancy needing investigation — but it's
    reported as a finding rather than silently assumed to match.
  - Vector-count arithmetic checked directly: 100 = 146 − 46, exact, no
    silent drift between the two counting methods.
  - Retrieved a real shared chunk (a LaTeX-package list common to both
    OS install pages) with `sources` containing both
    `installation/linux.html` and `installation/macos.html` URLs on one
    `chunk_id` — literal proof the merge path (entry #19) works on real
    duplicate content, not just the synthetic/archived cases.
  - 3 real queries: one whose answer is only in `circle_reference`
    (top-3 all `circle_reference`, correctly excluding the install
    pages); one whose answer is only in the install pages (top-3 all
    carry **both** linux and macos in `sources`, proving the match is
    the deduped cross-page chunk, not a coincidence of a one-page index);
    one genuinely absent from all indexed content ("capital of France,"
    distances 520–530 vs. 97–219 for the real matches — a real,
    usable gap, roughly 2.5–5x).
  - **Caveat worth carrying forward**: Chroma's default distance here is
    raw squared-L2, not cosine similarity — the absolute numbers aren't
    bounded or comparable across embedding models/dimensions. A future
    "no good answer" cutoff needs calibrating per model, not hardcoded
    as a universal constant from this one run.

### 24. JSONL file-marker dedup and Chroma content-hash dedup verified to coexist correctly under crash-resume
- `tests/test_crash_resume_dedup.py`, offline (temp JSONL file +
  `EphemeralClient`, stub embed_fn), 3 scenarios: (1) full success then a
  spurious retry — exactly 1 JSONL row, exactly 2 Chroma vectors (not 4),
  and zero re-embed calls on the retry (both chunks already existed with
  this source, so it's a pure metadata read). (2) the actual crash window
  entry #13 is about — JSONL append succeeds, crash before Chroma upsert
  — retry correctly skips the JSONL append (already-written) but *still*
  performs the Chroma upsert (confirmed: `Writer.write()`'s Chroma call
  isn't gated on the JSONL already-written check, so this doesn't silently
  drop the vectors). (3) the combined case with a real cross-page-duplicate
  chunk — write page A, retry page A, write page B sharing one chunk with
  A — exactly 2 JSONL rows (one per page, not per `write()` call), exactly
  1 Chroma vector for the shared chunk with both source URLs, and exactly
  1 embed call ever for that chunk. This is the exact scenario step 8's
  real kill test will exercise; verified structurally here first so step
  8 confirms it under real interruption rather than discovering a gap for
  the first time under pressure.

---

## 2026-08-18 — Step 7 close-out: cross-site extraction check before step 8

### 25. The Q&A extraction prompt held up on 3 new site shapes, with one real gap left unexercised
- **Why this check happened**: step 4's extraction validation (`LESSONS_LEARNED.md`
  #11) only ever ran on 2 pages, both Sphinx-generated Python docs from the
  same site (manim). That's the component producing the actual training
  data, it fails quietly (bad output looks plausible, no exception), and it
  had never been run against a fundamentally different site shape. Checked
  before step 8's real end-to-end run, not after.
- **Method**: 4 real calls, same prompt (`extraction.py::QA_EXTRACTION_SYSTEM_PROMPT`,
  byte-identical to step 4's) and same model (Ollama cloud, `deepseek-v4-flash`)
  as step 4 — site was the only variable. Ran the actual production
  extraction call shape (`main.py::_make_extract_fn` + `extraction.py::parse_qa_json`),
  not a resurrected copy of the retired `output_manager.py`. Sites: FastAPI's
  docs homepage (cached fixture, 24,528 stripped chars — substantial, not
  thin); a real Cloudflare blog article fetched for this check
  (`good-and-bad-agentic-behaviors/`, 15,914 stripped chars — the cached
  root fixture is a listing page, not a single article, so it couldn't
  have stood in for "prose" content); a real StackBlitz docs page fetched
  for this check (`developer.stackblitz.com/guides/user-guide/what-is-stackblitz`,
  6,721 stripped chars — the cached `stackblitz.com` root is a thin
  marketing page with no in-domain article of its own); and
  `www.manim.community`'s cached root as-is (5,369 stripped chars),
  deliberately kept as the thin-marketing-page case since step 6
  (`LESSONS_LEARNED.md` #17) already flagged this exact page as
  outranking real docs content for one intent — worth stress-testing here
  too, not swapped away. Both newly-fetched pages cached permanently under
  `tests/fixtures/`.
- **Result — no parse failures, no nav/chrome questions, no hallucination
  found on any of the 4 sites.** All 4 calls returned valid JSON on the
  first parse (no salvage path needed). Zero questions referencing
  navigation/menu structure on any site — the original `LESSONS_LEARNED.md`
  #4 failure mode did not reproduce outside the manim case that originally
  surfaced it. Spot-checked the two highest-risk-looking pairs against
  actual source text: FastAPI's "200% to 300% faster / 40% fewer bugs"
  claim and Manim's `SquareToCircle` code example (rotation angle, fill
  color/opacity, method call order) both matched the source verbatim —
  no invented structure in this sample.
- **The anticipated "vague prose" risk didn't materialize either, on this
  one sample.** Cloudflare's 5 pairs are specific and directly grounded
  in the article's own framing (Risk vs. Trust, the doorbell analogy,
  "hybrid" bot/human traffic) — not vaguer or lower-quality than the
  reference-page pairs. **Stated as a limit, not a conclusion**: this is
  one prose article, once — it doesn't prove prose sites are safe in
  general, only that this specific sample didn't trigger the concern.
- **Real gap, left open rather than papered over**: none of the 4 pages
  selected for this check lack markdown headings after stripping (checked
  before running any LLM call) — including the deliberately-thin
  `www.manim.community` root. So the "does extraction degrade on a page
  with no headings" question (motivated by `score_headings`'s no-headings
  fallback bug in `LESSONS_LEARNED.md` #17) remains **untested for the
  Q&A extraction path specifically** — #17's finding was against the
  relevance-scoring path, a different function entirely
  (`score_whole_page`'s fallback), not `_make_extract_fn`/`parse_qa_json`.
  A genuinely heading-less real page still needs to be run through
  extraction before this case is closed. See `ROADMAP.md`.
- **One real, un-asked-for finding**: FastAPI's pairs include an accurate
  (verified against source, not hallucinated) but low-value question
  about the page's sponsor list. Not a chrome-stripping or prompt defect
  — it's the 4000-char truncation window (`LESSONS_LEARNED.md` #10's
  established limit) landing on the page's sponsor-badge section, which
  happens to sit early in FastAPI's homepage markup, before the page's
  actual technical content further down. A page whose real content is
  front-loaded *after* low-value boilerplate will produce technically
  correct but low-value pairs regardless of how well chrome-stripping and
  the prompt otherwise perform — a distinct failure mode from both "nav
  junk" and "vague prose," worth naming separately. See `ROADMAP.md`.
- **Per instruction, the prompt was not touched in this step** — all 4
  sites produced usable output as-is, and retuning against 4 samples of
  one call each would risk overfitting in a new direction rather than
  fixing a demonstrated problem. The one real gap (no-headings case) and
  the one real finding (truncation-window content ordering) are recorded
  here and in `ROADMAP.md` for a future, evidence-driven fix — not fixed
  now.

---

## 2026-08-18 — Step 8 Part A: per-chunk extraction fixes reachability, not relevance or redundancy

### 26. per_chunk extraction reaches 84% more real content on a long page, but doesn't reduce low-value or duplicate pairs -- two separate problems it was never going to solve
- **Fix implemented**: `extraction_units.py::select_extraction_units()` +
  `config.EXTRACTION_STRATEGY` (default `PER_CHUNK`) replace the single
  `content[:4000]` extraction call with one call per parent-sized chunk
  (`config.PARENT_CHUNK_SIZE`/`_OVERLAP`, same units already used for
  Chroma). `TOP_K_CHUNKS_BY_RELEVANCE` is also implemented (embeds each
  chunk, keeps the top_k most similar to the intent) but not the default
  -- it needs an intent to rank against and falls back to `PER_CHUNK`
  without one. `FIRST_N_CHARS` (the old behavior) stays available as an
  explicit opt-in. `pipeline.py::extract_worker` now loops over units,
  tags each resulting pair with its `source_chunk` (feeds `canonical.py`,
  see #27), and only fails the whole page if *no* unit parsed -- one bad
  chunk's malformed JSON doesn't lose the rest of a page's real content.
- **Live re-run, same page (FastAPI homepage), same prompt/model as the
  cross-site check (#25)**: 18 calls instead of 1, 81 pairs instead of 5.
  **84% of the new pairs (68/81) cover content the old single-call
  window structurally could not reach** -- full sponsor/testimonial
  lists, Typer, the actual installation + code walkthrough, running the
  dev server, Swagger/ReDoc mechanics, the type-hint/Pydantic validation
  example, deployment, dependencies, license. This is the real payoff
  the truncation fix was for.
- **What it did NOT fix, found by the same re-run**: the "sponsors"
  question from #25 didn't disappear -- it multiplied (units 2-6, ~13
  pairs across keystone/gold/silver sponsors and testimonials, all
  low-value by the same standard #25 flagged). `per_chunk` fixes
  *reachability* (nothing past the old window is skipped); it does
  nothing about *relevance* (every chunk gets extracted regardless of
  value) -- a page that front-loads several low-value chunks before its
  real content produces more low-value pairs, not fewer. That's
  specifically what `TOP_K_CHUNKS_BY_RELEVANCE` exists to address and
  `PER_CHUNK` doesn't -- worth keeping distinct, not treating the
  reachability fix as if it were also a relevance fix.
- **Duplicate rate measured, not eyeballed, and found to have two
  separate causes**: answer-text similarity (not question-phrasing --
  question templates like "Who are the authors of..." repeat across
  unrelated topics and inflate a naive question-only measure) between
  pairs from the same or adjacent chunks only. Correction to the
  original framing: the extraction units are the *parent* chunks
  (2000/200 = 10% overlap), not the child chunks used for Chroma
  (400/200 = 50%) -- a different config, and 10% is what's actually in
  play for extraction.
  - **Adjacent-chunk boundary duplicates** (real chunk-overlap artifact):
    FastAPI 8/81 (~10%, concentrated at exactly the 2 chunk boundaries
    that happened to split a UI-mechanic explanation and a dependency
    list); Cloudflare's listing page 13/45 (~29%, higher because its
    dense teaser-block format packs more distinct facts per 200 char
    window, so more boundaries land mid-teaser).
  - **Same-chunk paraphrase duplicates** (not an overlap artifact at all):
    FastAPI 14/81 (~17%), Cloudflare 8/45 (~18%) -- the prompt's "3 to 5
    diverse pairs" instruction pads with rephrasings when one chunk's
    content only supports one real fact (e.g. 4 near-identical "who are
    the gold sponsors" pairs from a single sponsor-list chunk).
  - **Combined: ~27% of FastAPI's pairs, ~47% of Cloudflare's, are some
    form of near-duplicate** -- and none of them would be caught by
    export-time exact-question dedup (`export_formats.py::dedup_by_question`),
    since every one is a fresh generation with different wording.
    Deduping earlier would need semantic similarity, not exact-match
    normalization -- not built this step; see `ROADMAP.md`.
- **Heading-less page (blog.cloudflare.com's root listing, 0 headings,
  closing the gap #25 flagged), 45 pairs from 10 chunks**: 7 factual
  claims spot-checked directly against the stripped source text (DoD IL4
  commitment, FedRAMP certification, the Kitesurf description, WebMCP's
  launch, "only vendor named Visionary," the Agent Access Model
  description, the 519% DDoS stat) -- **all matched verbatim or
  faithfully, including one subtle case** (a pair correctly used the
  eclipse's own date, Aug 12, distinct from that post's own Aug 13
  publish date, both present in the source). No hallucination found in
  this sample. But the page's real content genuinely *is* mostly
  title/author/date teaser metadata for other articles -- so per-chunk
  extraction faithfully produces mostly "who wrote/when was X" trivia,
  not because it invented anything, but because that's most of what a
  listing page's markup actually contains. Not an extraction-quality
  problem; closer to "should a listing page pass `extract_threshold` at
  all" -- a relevance-gating question, not this step's to answer.
- **Why it matters**: three genuinely separate concerns got conflated in
  the original ask and needed separating by measurement:
  *reachability* (fixed this step), *relevance/low-value content* (not
  addressed by `per_chunk`, needs `top_k_chunks_by_relevance` + a real
  intent, or extract_threshold gating upstream), and *redundancy* (not
  addressed by anything built so far -- needs semantic dedup, which
  doesn't exist yet). Treating "more chunks reached" as "problem solved"
  would have missed the other two entirely.

---

## 2026-08-18 — Step 8 Part D kickoff: robots.txt Allow-override bug caught by the first real crawl against its own test site

### 27. stdlib urllib.robotparser resolves Allow/Disallow by file order, not specificity -- a step-5 test asserted the wrong answer and passed
- **Problem**: firing Part D's real crawl against `docs.manim.community/en/stable/`
  (deliberately chosen so the new pipeline's output would be comparable
  to the pre-rebuild audit) produced 469 `robots_disallowed` failures
  out of ~500 discovered URLs within the first few seconds -- almost the
  entire site, including the exact `/en/stable/` pages the crawl was
  supposed to fetch. `robots_disallowed` is permanent
  (`mark_permanently_failed`, no retry by design, `LESSONS_LEARNED.md`
  step-5 entries) -- every one of those rows was unrecoverable in that
  frontier.db. Caught immediately because 2 `done` vs. 469 `failed`
  after a few seconds was an obviously wrong ratio, not left to run to
  completion.
- **Root cause**: `docs.manim.community`'s real `robots.txt` is
  ```
  User-agent: *
  Disallow: /
  Allow: /en/stable/
  ```
  -- intentionally blocking every other doc version while explicitly
  allowing the current stable one. Per RFC 9309 (and every major real
  crawler), the *longest matching pattern wins*, regardless of which
  line appears first -- so `/en/stable/*` should resolve to allowed.
  stdlib `urllib.robotparser.RobotFileParser.can_fetch()` does not
  implement this: confirmed by direct reproduction
  (`parser.can_fetch('*', '.../en/stable/')` returns `False`) that it
  resolves rules in **file order** -- the blanket `Disallow: /` is
  checked first, matches every path, and returns before the more
  specific `Allow: /en/stable/` line is ever considered.
- **This bug was already visible in step 5's own fixture data and
  missed anyway**: `tests/fixtures/robots/docs_manim_community.json`
  (fetched during step 5) already recorded
  `"disallows_root_for_star": true` for this exact site. The dedicated
  test, `test_docs_manim_community_disallows_root_for_everyone`, even
  had a comment reading "everything blocked except whatever's explicitly
  allowed underneath it" -- correctly describing the semantics -- and
  then asserted `assertFalse(policy.is_allowed(".../en/stable/"))`,
  the literal opposite of what its own comment said, checking the one
  URL that was supposed to be allowed. The test passed because
  robotparser's buggy behavior happened to agree with the wrong
  assertion. Nobody re-read the assertion against the comment it sat
  next to, and the fixture-based test suite recorded the robots.txt's
  *shape* but never actually exercised the one path that mattered until
  a live crawl against this exact site did.
- **Fix**: `robots_cache.py` no longer delegates to
  `urllib.robotparser` at all -- `_parse_rules()`/`_is_path_allowed()`
  are a from-scratch implementation of RFC 9309's group-selection and
  longest-match-wins resolution (wildcard `*` and end-anchor `$`
  supported; an equal-length Allow/Disallow tie resolves to Allow, the
  less restrictive rule, per spec). `HostPolicy.is_allowed()` dropped
  its now-vestigial per-call `user_agent` parameter in the same change
  -- rule-group selection already happens once at `get_policy()` time
  against `RobotsCache`'s configured agent, so a parameter that looked
  like it selected a group per call but silently didn't would be a
  worse API than removing it. Verified against the real live
  `docs.manim.community/robots.txt` directly (not just the cached
  fixture): `/en/stable/` and a real reference page under it now
  resolve `True`; `/en/latest/` and bare `/` still correctly resolve
  `False` -- the override is specific to the one allowed prefix, not a
  blanket allow.
- **The wrong test was rewritten, not deleted**, with the real failure
  mode as its docstring, plus new dedicated unit tests in
  `tests/test_robots_cache.py` for: order-reversed Allow/Disallow (same
  outcome either way, proving it's resolved by specificity and not "last
  line wins," which would have coincidentally fixed the original case
  for the wrong reason), wildcard/end-anchor patterns, an equal-length
  tie, and named-user-agent-group preference over `*`.
- **Why it matters**: this is exactly the failure mode "verify, don't
  assume" exists to catch, and it slipped through anyway because the
  verification itself (the fixture test) asserted the wrong ground
  truth with a comment that contradicted its own assertion sitting right
  next to it. A fixture capturing a site's robots.txt *shape*
  (`disallows_root_for_star: true`) is not the same as verifying the
  crawler's *decision* on the specific paths it will actually request --
  the gap between those two is exactly where this bug lived,
  undetected, since step 5. Any future robots.txt test should assert
  the decision for the real paths the crawler will hit, not just record
  what the file looks like.

---

### 28. chrome_strip.py was never called from main.py's real fetch path -- built, tested, and validated entirely in isolation since step 4
- **Problem**: the 47 nav-menu questions found on Part D's real
  docs.manim.community crawl (entry #27's sibling finding) traced back
  to something much bigger than a `chrome_strip.py` selector gap:
  `main.py::_make_fetch_fn` built `CrawlerRunConfig` with only
  `cache_mode=CacheMode.BYPASS` -- no `excluded_tags`, no
  `excluded_selector` -- and handed `result.markdown` straight to
  extraction and Chroma chunking, completely unstripped. Confirmed by
  grep, not inference: `strip_chrome()`/`clean_html()`/
  `strip_text_patterns()` are called nowhere outside `chrome_strip.py`
  itself and its own test file, across the entire codebase.
- **Every prior "chrome-stripping validated" claim was true only for a
  hand-rolled call to `strip_chrome()`, never for `main.py` itself**:
  step 4's fixture measurements, step 4's two-page extraction test, step
  6's chrome-stripped extraction test, step 7's live demo, and step 8's
  cross-site extraction check (`LESSONS_LEARNED.md` #25) all called
  `chrome_strip.strip_chrome(html, url)` directly from a standalone
  script against cached fixture HTML. None of them ever ran `main.py`
  itself end to end. Part D -- the first real crawl through the actual
  CLI -- is the first time this gap could have been visible, and it was,
  immediately.
- **Root cause of the root cause**: verification always happened one
  layer below the actual entry point. Component-level testing
  (`tests/test_chrome_strip.py`) and ad hoc integration checks (steps
  4/6/7/8's demo scripts) both exercised `strip_chrome()` correctly and
  thoroughly -- the function itself was never wrong. What was never
  tested is that `main.py` *calls* it. This is the second time in the
  same session a real end-to-end run caught something careful
  component-level testing missed (see #27, the robots.txt precedence
  bug) -- both times, the missing layer was integration, not coverage.
- **Fix**: `chrome_strip.py`'s own docstring says `strip_chrome()`
  "mirrors what `CrawlerRunConfig` does for the real pipeline" -- meaning
  the *intended* production path was always to pass
  `excluded_tags=DEFAULT_EXCLUDED_TAGS`,
  `excluded_selector=DEFAULT_EXCLUDED_SELECTOR` into the real
  `CrawlerRunConfig` directly (crawl4ai's own HTML-cleaning stage,
  applied once, during the real fetch) rather than re-running
  `clean_html()` + `DefaultMarkdownGenerator()` a second time on
  already-converted markdown. `_make_fetch_fn` now does exactly that,
  plus applies `strip_text_patterns()` (layer 2) to the resulting
  markdown before it reaches extraction or chunking -- the same two-layer
  pipeline `strip_chrome()` runs internally, just through crawl4ai's
  native mechanism instead of a redundant second HTML pass.
- **New test asserts through the real entry point, not through
  `strip_chrome()` again**: `tests/test_fetch_fn_integration.py` calls
  `main._make_fetch_fn()` directly against a fixture HTML string
  containing nav/aside/footer/button chrome, using crawl4ai's `raw:` URL
  scheme (no network call, but the real `AsyncWebCrawler.arun()` +
  `CrawlerRunConfig` + `LXMLWebScrapingStrategy` pipeline genuinely
  runs) and asserts the chrome text is absent from the output.
  Confirmed this test actually catches the original bug: re-ran the
  same fixture through the unfixed config (no `excluded_tags`) and the
  nav text came through, as expected.
- **A systematic audit followed, checking every other module for the
  same class of gap** (defined, tested, never actually called from
  `main.py`'s reachable path): relevance scoring, `normalize_url`,
  `is_in_scope`, `derive_prefix`, robots checks, the writer's dedup
  marker, content-hash chunk IDs, sectioning, and canonical record
  building were all confirmed genuinely wired, by tracing real call
  sites, not by recalling that they should be. Two categories found
  that are *unreachable from `main.py` but not bugs*: everything in
  `export.py`/`export_formats.py` beyond `load_canonical_records`/
  `dedup_by_question` (by design -- a separate CLI entry point per the
  step 8 Part B/C plan) and `relevance.py::score_max_chunk` (a measured
  and deliberately-rejected scoring strategy, `LESSONS_LEARNED.md` #17).
  Three minor, behaviorally-inert dead surfaces found alongside:
  `RobotsCache.is_allowed(host, url)` (crawl_worker calls
  `HostPolicy.is_allowed()` directly instead -- the check still runs,
  just via a different object), `Frontier.get_all_urls()` (test-only),
  and `LocalOllamaEmbeddings.embed_documents()` (the batch method --
  only single-text `embed_query` is ever used). None of these three
  have a behavioral consequence; `chrome_strip.py` was the only real gap.
- **Why it matters**: "verify, don't assume" needs a specific target --
  verifying a function works is not the same claim as verifying it's
  called. A test suite can have excellent coverage of every individual
  piece and still have zero coverage of whether those pieces are wired
  together the way the surrounding prose says they are. The fix for that
  blind spot isn't more unit tests of already-tested functions; it's at
  least one test per pipeline stage that goes in through the same door a
  real user would.

---

### 29. Part D complete: resumability proven on real data, and a controlled before/after on the chrome-stripping fix
- **Resumability, on the actual production path for the first time**:
  killed the crawl (hard TaskStop, not a graceful shutdown) at 10 done /
  15 in_progress against a real `docs.manim.community` crawl, restarted
  with identical CLI inputs. All 10 `done` URLs stayed `done` (never
  re-fetched); all 15 stuck `in_progress` rows were recovered (11
  re-claimed, 4 back in `queued`, none lost or silently dropped). One
  page's write had genuinely raced the crash window (JSONL append
  committed, frontier status update didn't) -- occurred naturally, not
  fabricated -- and on retry produced neither a duplicate JSONL row (96
  rows before and after) nor a duplicate Chroma vector (0 vectors with a
  repeated source entry, 1445/1445 unique ids). This is the step-5
  crash-window design and the step-7 content-hash IDs exercised together
  against real interruption for the first time, not just the synthetic
  scenarios in `tests/test_crash_resume_dedup.py`.
- **`max_pages` overshoots under real concurrency, not just across a
  resume -- a distinct case from the one `CLAUDE.md` already
  documents.** `claim()`'s cap check
  (`done+skipped_extract < max_pages`) is correct in isolation, but it
  only gates *new* claims -- it can't cancel rows already claimed and
  mid-flight. With 3 crawl workers racing far ahead of 2 slow (per_chunk,
  multi-call) extract workers, many rows can be simultaneously
  `in_progress` while `done` is still well under the cap; all of them
  finish and increment `done` regardless, so the real ceiling with
  `max_pages=20` and this concurrency profile was 35, both times this
  ran. Not the same gap as the already-documented "resumed run keeps
  claiming past the cap" case -- this one happens within a single
  uninterrupted run. Not fixed here; `ROADMAP.md` should get an entry
  before this surprises someone sizing a `max_pages` value for cost
  control expecting a hard ceiling.
- **The controlled before/after the site choice was for**: full numbers
  and the two bugs this run caught (#27 robots.txt, #28 chrome-strip
  wiring) are in the published artifact
  (part_d_report.html) rather than duplicated here. Headline: nav-menu
  questions 47 → 0, exact-duplicate questions 12.5% → 0.09% of pairs,
  pairs/page 86.5 → 32.7, near-duplicate-affected rows 80.1% → 54.8%,
  char survival 9.2-33.6% on 3 sampled pages (consistent with step 4's
  original 21-68% range on other sites). Vectors/page barely moved
  (41.3 → 46.7, both far under the archive's 306.8) for a real, checked
  reason, not a wash: before the fix, content-hash dedup was mostly
  collapsing *repeated chrome* (85.6% collision reduction, byte-identical
  sidebar text across all 35 pages); after, there's far less raw content
  overall but what remains is genuine per-page content that doesn't
  collide across pages nearly as much (3.3% collision reduction). Both
  effects are real and roughly cancel in the per-page count, while the
  *composition* of what's stored changed completely.
- **The `other` redundancy cause (non-adjacent, same-page) is confirmed
  structural, not a chrome artifact** -- it stayed dominant at almost the
  same share before and after stripping (68.6% → 71.6% of near-duplicate
  answer pairs). Chrome-stripping reduced every cause's absolute count
  roughly proportionally without changing which one dominates. This
  matches the step 8 Part A hypothesis (structurally repetitive
  API-reference content -- parameter lists, method signatures -- spread
  across far-apart chunks of a long page) and rules out "it was chrome
  all along" as an alternative explanation, now that a real before/after
  exists to check it against.
- **`dataset_report.py` needed a real performance fix, not just a
  documented ceiling.** The ponytail comment on `find_near_duplicates`
  originally said "fine for a capped crawl's few hundred rows" -- true in
  aggregate, false per-page: `NumberLine.html` alone produced 152 pairs
  before the fix, and O(n^2) `SequenceMatcher.ratio()` at that size
  timed out past 2 minutes for the whole report. Fixed with a
  `quick_ratio()` pre-filter (a documented difflib pattern -- cheap upper
  bound, skip the full O(n*m) `ratio()` unless it already clears
  threshold) rather than raising the timeout and hoping smaller crawls
  stay smaller.
- **Why it matters**: this step produced two structural bug fixes (#27,
  #28), a real crash-resume proof, a previously-undocumented `max_pages`
  overshoot mode, a confirmed-not-refuted hypothesis about redundancy's
  root cause, and a real performance fix to the tool built to measure
  all of it -- from one controlled run against a site chosen specifically
  because there was archived data to check against. That is what "run it
  for real" was for.

---

## 2026-08-19 — Step 8 Phase 2A: sectioning was depth-from-root, not depth-from-seed -- every page in the real corpus had the same section

### 30. derive_section counted depth from the domain root, not the crawl's own seed prefix -- confirmed by reading the real corpus, not by inspecting the code
- **Problem**: every one of the real Part D corpus's 1144 records had
  the identical `"section": "en/stable"` -- found by reading a sample of
  `data/run/canonical.jsonl` directly, not by re-deriving expectations
  from the code. `derive_section(url, depth=2)` counts `depth`
  non-empty path segments from the URL's root, with no awareness that
  the crawl was seeded at `/en/stable/` -- for `docs.manim.community`,
  the locale (`en`) and version (`stable`) segments the seed itself
  sits behind consume the entire depth budget before any real category
  (`reference`, `tutorials`, `changelog`, ...) is ever reached. Every
  page produced the same section regardless of its real content, and
  Part C's export-time filename logic (slugify, cap+hash,
  disambiguate, `manifest.json`) had never seen more than one distinct
  label -- a per-section export would have produced exactly one file.
- **Fix**: `content/sectioning.py::derive_section()` gained an optional
  `seed_prefixes: list[tuple[str, str | None]]` parameter -- the exact
  same `(host, prefix)` list `main.py` already builds for `scope_check`
  (`crawl/scope.py::derive_prefix` per selected branch), reused rather
  than reimplemented. When a URL's host+path matches one or more
  prefixes, the *longest* match wins (same convention `crawl/scope.py`
  itself uses) and the path is made relative to it before segmenting;
  a crawl with multiple seeds on different prefixes is handled the same
  way, not just the single-seed case. `seed_prefixes=None` (the
  default) falls back to the original root-relative behavior, so
  existing callers (tests, or a URL that matches no known seed) don't
  break. Wired through `crawl/pipeline.py::extract_worker`'s new
  `seed_prefixes` parameter, populated from `main.py`'s existing
  `host_prefix_pairs`.
- **The existing corpus was repaired in place, not re-crawled**: a
  one-off offline script re-derived `section` for all 1144 records from
  their already-known `source_url` + the run's actual seed prefix
  (`docs.manim.community`, `/en/stable/`) and rewrote
  `data/run/canonical.jsonl`. Re-crawling 35 pages again just to fix a
  metadata field would have spent real LLM calls for no reason --
  `derive_section` is a pure function of information the corpus already
  has.
- **Depth measured at 1, 2, and 3 (seed-relative) against the real,
  repaired corpus, not guessed**: depth=1 produces exactly the 6 clean
  categories expected (`reference` 812, `tutorials` 100, `guides` 91,
  `changelog` 67, `conduct.html` 65 -- a genuine single-segment page, not
  a bug, `installation` 9). depth=2 and depth=3 are *identical* to each
  other (35 sections each) and degenerate to near-per-page granularity,
  because manim's real URL structure never nests past 2 segments past
  the seed -- `reference/<leaf>.html` is as deep as it goes. `config.
  SECTION_DEPTH`'s default (2) was deliberately left unchanged in this
  fix -- whether 1 is a better default for typical doc sites is a
  separate decision from making the counting correct, same discipline
  as every other "make visible, don't retune in the same commit"
  decision this project has followed. Worth revisiting with more than
  one site's real structure before changing the default.
- **A second, independent bug found while measuring**: `export/export.py`'s
  `--section-depth` CLI argument is accepted and threaded through to
  `package_plain_jsonl(..., section_depth)` but **never actually used**
  inside that function -- sections are grouped purely by the `section`
  field already baked into each canonical record at crawl time. Passing
  `--section-depth 1` vs. `--section-depth 3` to a real export call
  produces byte-identical output. Depth is crawl-time-only; there is
  currently no way to change section granularity at export time without
  re-deriving `section` on the canonical file first (exactly what the
  repair script above did, ad hoc). Not fixed here -- see `ROADMAP.md`.
- **Real corpus exercised the length-cap+hash path but never a real
  collision -- constructed a synthetic case rather than declaring
  Part C verified anyway**: at depth 2/3, 5 of 35 real section labels
  were long enough to hit `cap_slug`'s 60-char truncation-with-hash-
  suffix (e.g. `reference-manim-utils-docbuild-manim-directive-setu-
  df7fa229`), confirmed by inspecting the real filenames produced, not
  assumed from the cap constant. But 0 collisions occurred at any depth
  (`disambiguate_slugs` never needed to append a numeric suffix) --
  real Sphinx URL slugs happened not to collide after slugification.
  Per the explicit instruction not to declare this verified on
  incomplete evidence, constructed a synthetic 3-row canonical file
  with three section labels that genuinely collide after slugification
  (`a!b`, `a?b`, `a.b` all -> `a-b`) and ran it through the real
  `run_export` -> `package_plain_jsonl` path (not just
  `disambiguate_slugs()` in isolation) -- produced `a-b.jsonl`,
  `a-b-1.jsonl`, `a-b-2.jsonl`, three separate files each with their
  own row, not a silent 3-into-1 merge. Kept as a permanent regression
  test (`tests/test_export.py::test_plain_jsonl_disambiguates_a_real_
  slug_collision_not_just_the_unit`) rather than a throwaway diagnostic,
  since a real collision is exactly as hard to find on demand as the
  cross-page chunk duplicate was in step 7.
- **Other Part C claims checked against the real (repaired) corpus**:
  longest absolute path produced was 110 characters
  (`D:\scraper\data\export\...\reference-manim-utils-docbuild-manim-
  directive-setu-df7fa229.jsonl`) against Windows' 260-char limit -- 150
  characters of headroom, not tight. `unified.jsonl`'s row count and the
  sum of every per-section file's row count matched exactly (1143 = 1144
  raw rows minus 1 exact-duplicate question) at every depth tested --
  no rows silently dropped or double-counted by the per-section split.
- **Why it matters**: this is the same shape of finding as #27/#28 --
  something built and unit-tested correctly in isolation
  (`disambiguate_slugs`, `cap_slug`, `derive_section` itself) had never
  been exercised against data that actually stressed it, and the gap
  was invisible until real data was read directly rather than assumed
  correct because the code looked right and the tests passed.

---

## 2026-08-19 — Step 8 Phase 2B-2D: three findings from reading the real canonical.jsonl directly (2A is entry #30, above)

### 31. Nearly half of every chunk's characters are markdown link syntax, not content -- the largest single finding of this phase
- **The number, measured across all 264 real chunks (deduped by
  source_url+chunk_index, not per-pair)**: **46.7% of all chunk
  characters sit inside markdown link syntax** `[text](url)`. Of that,
  the *visible* link text a reader would actually see is only **2.9%**
  of total chars -- the remaining **43.8 percentage points is pure URL +
  syntax overhead**. Not a few outlier chunks skewing an average: mean
  45.6%, median 45.0% per chunk, and it reaches 89.5% on the worst one.
  A concrete real example, not an abstraction: a font-template
  reference table where every row is `` [`biolinum`](https://docs.
  manim.community/.../TexFontTemplates.html#....biolinum
  "manim.utils.tex_templates.TexFontTemplates.biolinum") `` -- **8
  characters of actual signal (the symbol name) wrapped in roughly 230
  characters of repeated full URL and a duplicated title attribute
  identical to the anchor fragment.**
- **Why this dwarfs the redundancy problem in impact**: this isn't a
  content-quality issue affecting some pairs -- it's a character-budget
  tax on *everything* that touches this text. Every embedding call
  (relevance scoring, Chroma child chunks), every extraction LLM call
  (`content[:MAX_EXTRACT_CHARS]` truncation included), and every
  chunk's `PARENT_CHUNK_SIZE`/`CHILD_CHUNK_SIZE` budget is spending
  roughly half its allotment on link machinery instead of real page
  content. On a reference-heavy site like this one, that means
  `MAX_EXTRACT_CHARS`/`MAX_EMBED_CHARS` effectively cover about half
  the real content their raw character counts suggest, and it's a
  significant, previously-unquantified contributor to the original
  audit's 9,204-vectors-from-30-pages figure -- more chunks were needed
  partly because each chunk carried less actual content than its length
  implied. Not fixed here -- see `ROADMAP.md`, ranked above the
  near-duplicate work precisely because of this scale.

### 32. chrome_strip.py misses content that is neither semantically marked nor visually rendered -- two distinct mechanisms, both confirmed
- **Root cause 1 -- invisible SVG icon-sprite `<title>` text.** Furo's
  theme ships an `<svg style="display:none">` block near the top of
  `<body>` containing icon `<symbol>` definitions, each with a
  `<title>` (`<title>Light mode</title>`, `<title>Expand</title>`,
  etc.) -- accessibility labels for icons referenced later via
  `<use href="#svg-sun">`, never rendered themselves. crawl4ai's
  markdown conversion extracts this title text as real content anyway,
  producing exactly the "Contents Menu Expand Light mode Dark mode Auto
  light/dark, in light mode Auto light/dark, in dark mode" sequence
  found at the start of `image_mobject.html`'s chunk 0 (and confirmed
  present in every other chunk-0 sampled). `svg` isn't in
  `DEFAULT_EXCLUDED_TAGS`, and CSS `display:none` isn't something the
  structural-exclusion mechanism evaluates -- it works on tag/role
  presence, not computed visibility.
- **Root cause 2 -- bare utility links with no landmark tag or ARIA
  role.** `<a class="skip-to-content muted-link"
  href="#furo-main-content">Skip to content</a>`, plus "Back to top" /
  "View this page" / "Edit this page", sit in plain `<div>`/`<label>`
  wrappers -- not `<nav>`/`<header>`/`<aside>`/`<button>`, and no
  `role="navigation"` or equivalent. Neither `DEFAULT_EXCLUDED_TAGS`
  nor `DEFAULT_EXCLUDED_SELECTOR` has anything to match against.
  Verified both mechanisms survive the real `strip_chrome()` call
  end-to-end (not just in raw HTML) against the cached reference
  fixture.
- **General lesson, stated for the next time this category of gap
  shows up**: structural exclusion (tag names) and semantic exclusion
  (ARIA roles) both assume chrome is *marked* as chrome, one way or the
  other. Content that's neither semantically labeled (no role, no
  landmark tag) nor actually visible (`display:none`, referenced only
  indirectly) falls through both nets at once -- it isn't hiding in a
  gap between the two mechanisms, it's outside what either mechanism
  was ever designed to see. Not fixed here -- reported per the explicit
  ask to measure before proposing anything.

### 33. Overlap and padding measured, and a real decision made not to build split chunking
- **2B (same-chunk paraphrase padding)**: corpus-wide, 98.9% of 264
  chunks produced 4 or 5 pairs regardless of content richness --
  essentially none produced 1 or 2. Official near-dup accounting: 380
  same-chunk near-duplicate answer pairs, touching 52.7% of chunks and
  37.8% of rows. A controlled 12-chunk real A/B test (old prompt vs. a
  variable-count + explicit-anti-reword prompt, reusing the exact
  stored `source_chunk` text for `building_blocks.html` chunks 19/22 --
  the cited examples -- plus 4 more chunks and 6 from the Circle
  reference fixture, zero new fetches) cut pair count 56 -> 43 (-23%).
  Per-chunk read of every reduction: 6 of 7 reduced chunks lost pure
  restatement with no real content lost (one, `building_blocks#10`,
  going from 4 near-identical rewordings of one fact down to a single
  complete answer); one chunk (`building_blocks#22`) kept the same pair
  count but reallocated toward a fact the old prompt had missed
  entirely (play()/wait()) instead of two separately-worded pairs about
  the same Scene-role fact -- better coverage, not just fewer pairs.
  One chunk (`building_blocks#2`) showed a small, real loss: a pair
  about whether plain `Mobject` is commonly used carried minor practical
  framing beyond pure restatement. No prompt change applied to
  production code -- this was the measurement, not the fix.
- **2C (adjacent-chunk overlap)**: confirmed and traced precisely --
  the cited `np.roll` question in `building_blocks.html` chunk 22 draws
  from the tail of chunk 21's code example, present in chunk 22 only via
  the 200-char parent overlap; chunk 21 already had 2 pairs about the
  same code. Quantified directly (matched the real overlap substring
  between every consecutive same-page chunk pair, checked what fraction
  of each pair's answer is contained in that substring specifically):
  **24 of 1144 pairs (2.1%) draw >=50% of their answer's distinctive
  content from an overlap region.** `dataset_report.py`'s
  `adjacent_chunk`-classified near-duplicates (a related but broader
  measure -- two separate pairs from neighboring chunks resembling each
  other, not necessarily both drawing from the shared text) sit at
  300/2396 (12.5% of near-duplicates) -- same direction, same
  conclusion: real, but roughly 15x smaller than 2B's same-chunk
  padding by pair count.
- **Decision: do not build split chunking (extraction skips overlap,
  retrieval keeps it) -- evaluated, not implemented, per the explicit
  ask.** At a measured 2.1% direct-overlap-pair rate, the fix's
  benefit is small. The cost is real: a zero-overlap extraction chunker
  reintroduces a smaller version of the exact boundary-truncation
  problem step 8 Part A fixed (content straddling a chunk edge could go
  fully unextracted on both sides, not just duplicated), plus a second
  chunking config to keep in sync with the first indefinitely. **Chosen
  direction instead: pair-level semantic dedup** (already flagged as
  unbuilt in `ROADMAP.md` #23) -- it catches 2B's same-chunk padding
  and 2C's overlap duplication in one mechanism, regardless of which
  structural cause produced a given near-duplicate pair, without
  touching the chunking architecture at all. Not built this step --
  the decision is the direction, not an implementation.

## 2026-08-19 — Phase 3 Step 1: link-text normalization

### 34. Link syntax reduced to visible text, URLs dropped entirely -- tested against three options on the two cited examples first
Phase 2 measured link syntax at 46.7% of chunk characters (43.8 points
pure overhead) and reordered Phase 3 to fix this before anything else
that touches chunk content, so nothing downstream gets measured twice.

**Three options tested on the actual cited examples** (TexFontTemplates
chunk 2, 89.5% link syntax; `building_blocks.html` chunk 5, a tutorial
prose chunk) before choosing, per the explicit ask:
- **A -- drop the URL, keep visible text.** TexFontTemplates chunk 2:
  1747 -> 271 chars (15.5% of original). Tutorial chunk 5: 1182 -> 717
  chars (60.7%). Every symbol name (`biolinum`, `Scene`, `add()`)
  survived as plain text; nothing informative was lost in either
  example.
- **B -- per-chunk reference list** (citation markers inline, URLs
  moved to a trailing block). Barely reduced size: TexFontTemplates
  1747 -> 1393 chars (79.7% retained), tutorial 1182 -> 1047 (88.6%
  retained). Almost every link in these chunks points at a *different*
  URL, so a reference list has nothing to deduplicate -- it just moves
  the same bytes to the bottom of the chunk instead of removing them.
- **C -- keep the URL only where visible text is uninformative**, fall
  back to the link's `title` attribute otherwise. Reproduced option A's
  savings on both good examples, but on the heading-anchor pilcrow
  case (`[¶](... "Link to this heading")`) it fell back to the title
  and printed **"Link to this heading"** literally into the chunk --
  reintroducing exactly the boilerplate normalization exists to
  remove. The fallback also needs an ever-growing blocklist of
  theme-specific title phrases to stay clean, which conflicts with the
  no-site-specific-logic requirement more than option A's plain
  generic-text list does.

**Chosen: option A.** Every link actually observed in the real corpus
either duplicated its own visible text (a symbol name linking to its
own definition) or pointed at a same-page anchor -- the URL never
carried a fact the text didn't already carry, and this project's Q&A
pairs and RAG chunks are never rendered as clickable links, so a
literal URL string has no consumer downstream (`source_url` is already
a separate canonical-record field, independent of chunk text). The
theoretical risk flagged before testing -- two identically-worded links
in different places pointing at different targets, so the text alone
becomes ambiguous -- did not appear in either real example, and no
mechanism here would detect it if it did; noted as an open risk, not
built around.

**Implementation**: `content/chrome_strip.py::normalize_link_text()`,
new layer 2 in the `strip_chrome()` pipeline (structural strip -> link
normalization -> text-pattern fallback, renumbered from two layers to
three). `DEFAULT_GENERIC_LINK_TEXT` (pilcrows, "click here", "read
more", etc.) is a plain, non-site-specific list, overridable via a
`generic_text` param -- same convention as `DEFAULT_TEXT_PATTERNS` in
the same file, not a new config.py knob. A link whose text is empty,
has no alphanumeric content, or matches the generic-text list is
dropped entirely (both text and URL); every other link keeps its
visible text and drops the `(url "title")` syntax. Images (`![...]`)
are left untouched -- alt text without a `src` isn't independently
useful, so there's nothing to unwrap.

Wired into the real fetch path, not just tested in isolation --
`main.py::_make_fetch_fn` now calls `normalize_link_text()` on
`result.markdown` before `strip_text_patterns()`, the same lesson from
entry #28 applied proactively this time: define it, test it, *and*
confirm the real entry point calls it, in the same step, not three
separate ones. `tests/test_fetch_fn_integration.py` gained an assertion
that a real content link survives as text with its URL gone, through
the actual `AsyncWebCrawler` path, not `strip_chrome()` called
directly.

## 2026-08-19 — Phase 3 Step 2: the two strip_chrome root causes from entry #32

### 35. Both fixes reused existing generic mechanisms -- no new preprocessing code
Entry #32 diagnosed two distinct gaps: invisible `display:none` SVG
icon-sprite `<title>` text, and bare utility links with no landmark tag
or ARIA role. Both are fixed by extending the *data* behind mechanisms
that already existed, not by adding new code paths -- consistent with
this file's structural/text-pattern layering, which was already built to
be extended this way.

- **Root cause 1 (invisible content)**: added `[hidden]`,
  `[style*="display:none"]`, `[style*="display: none"]` to
  `DEFAULT_EXCLUDED_SELECTOR` -- the same structural-exclusion mechanism
  that already removes `[role="navigation"]` etc., via crawl4ai's own
  CSS-selector-based scraper. Verified the two `style*=` clauses catch
  every realistic spacing/ordering variant (`display:none`,
  `display: none;`, `color:red;display:none;`, `display: none;
  margin:0`) without needing a full CSS parser. This generalizes past
  SVG specifically -- any element the page marks invisible by CSS or the
  native `hidden` attribute is now excluded, not just icon sprites.
- **Root cause 2 (unmarked utility links)**: added
  "skip to content"/"back to top"/"edit this page"/etc. to
  `DEFAULT_TEXT_PATTERNS` (the existing text-pattern fallback) **and**
  to `DEFAULT_GENERIC_LINK_TEXT` (step 1's new link-text list, since
  these arrive as markdown link text before `strip_text_patterns` ever
  runs). The phrases are Furo's copy but not Furo-specific vocabulary --
  "skip to content" and "back to top" are standard UI copy across many
  doc themes, which is exactly the kind of extension `DEFAULT_TEXT_PATTERNS`
  was already designed for (see its docstring). No selector was written
  against Furo's actual class names (`skip-to-content`,
  `edit-this-page`) -- matching those would have been the site-specific
  shortcut the task explicitly ruled out.

**Found and fixed a third, unrelated bug while verifying against the
real fixture**: step 1's `normalize_link_text()` regex used
`[^\]]*` for the link-text group, which cannot match past an internal
`]` -- and Sphinx's auto-generated "view source" links render as
`[[source]](url)`, a bracketed label inside markdown link syntax. That
construct silently failed to match at all, leaving the full
`[[source]](https://...)` untouched in `strip_chrome()`'s output against
`tests/fixtures/docs_manim_reference.html` (confirmed: the literal
`_modules/.../arc.html#Circle` URL survived in the real fixture's real
output before this fix). Regex changed to allow one level of nested
`[...]`: `(?:[^\[\]]|\[[^\[\]]*\])*`. This is exactly the class of gap
entry #28 warned about -- caught here by testing against real fixture
content instead of only synthetic examples, before Step 1 was assumed
complete.

**Verification, both against the real fixture and a synthetic
regression guard**: confirmed end-to-end against
`tests/fixtures/docs_manim_reference.html` (the actual page entry #32's
diagnosis was read from) that every one of "Light mode", "Dark mode",
"Auto light/dark", "Skip to content", "Back to top", "View this page",
"Edit this page", "Expand", "Menu" is absent from `strip_chrome()`'s
output, with real page content (the `Circle` class docstring) intact.
`tests/test_fetch_fn_integration.py` gained
`TestRealFetchFnStripsChromeFromChunkedOutput`, built to satisfy the
explicit ask to check `source_chunk`-level text, not just extraction
questions: it runs a Furo-shaped fixture (the display:none sprite +
unmarked skip-to-content/back-to-top/edit-this-page links, same shape as
the real page) through the real `_make_fetch_fn` *and* the real
`select_extraction_units()` chunker, then asserts every chrome phrase is
absent from every resulting chunk. Confirmed this test fails against the
pre-fix selector/pattern lists (all five phrases leak into chunks) before
trusting it as a real regression guard, not just a test that happens to
pass.

## 2026-08-19 — Phase 3 Step 3: applied the 2B variable-count prompt to production

### 36. Prompt swap applied as measured, with the one known regression case recorded rather than left to be rediscovered
`content/extraction.py::QA_EXTRACTION_SYSTEM_PROMPT` now uses the exact
prompt text A/B tested in entry #33's 2B measurement, unchanged from what
was tested: "up to 5" instead of a fixed "3 to 5" pair count, an explicit
instruction to generate one pair per genuinely distinct fact rather than
padding to a target number, and an explicit self-check rule ("check it
against the ones you've already written -- if it's really the same
question reworded, skip it instead") before adding a pair. Measured
effect on the 12-chunk real test: 56 -> 43 pairs (-23%), 6 of 7
reductions being pure restatement removal or improved coverage at the
same count.

**Known regression, so it isn't rediscovered later**: `building_blocks#2`
showed a small real loss under the new prompt -- a pair about whether
plain `Mobject` is commonly used carried minor practical framing beyond
pure restatement, and the new prompt's stricter anti-reword rule dropped
it. This is a one-chunk anecdote from a 12-chunk test, not something to
chase with more prompt tuning right now; recorded here so if it shows up
again at scale in the Step 6 re-run, it's a confirmation of a known
tradeoff, not a new finding.

No test asserts the prompt's exact wording (none did before this
change either) -- extraction behavior against a live LLM isn't something
the offline test suite exercises; the real check is the Step 6 re-run's
pairs-per-chunk distribution against the Part D baseline.

## 2026-08-19 — Phase 3 Step 4: pair-level semantic dedup (ROADMAP #23)

### 37. Reused the already-calibrated near-dup detector instead of building a second one, kept it off by default
Decisions made and the reasoning, per the explicit ask to state them:

- **Where it runs**: export time only, as a new optional stage in
  `export/export_formats.py` between `dedup_by_question` (exact question
  match) and `split_records`. Not extraction time -- catching entry
  #33's 2C (adjacent-chunk overlap) requires comparing pairs from
  *different* chunks of the same page, and those come from separate,
  independent LLM calls in different `extract_worker` invocations with
  no shared state between them (and no legitimate way to give them one:
  `test_writer_ownership.py` structurally forbids `extract_worker` from
  taking a `Writer`/shared-state parameter, by design). A single
  export-time pass over the whole canonical file naturally sees every
  pair from every chunk of every page at once, which is exactly what
  "covers both causes in one mechanism" (the reason split chunking was
  rejected) requires.
- **Similarity measure**: `SequenceMatcher.ratio()` on normalized answer
  text, restricted to same-`source_url` pairs, `quick_ratio()`-prefiltered
  for the O(n^2) cost -- not a new embedding-based measure. This is
  `export/dataset_report.py::find_near_duplicates()`, unchanged, moved
  to `export_formats.py` (dataset_report.py now imports it back) so
  `export_formats.py`'s new `semantic_dedup()` could call it without a
  circular import. Reusing it rather than writing a second detector: it
  was already calibrated against real Part A data (LESSONS_LEARNED.md
  #26) and is exactly the measure that produced entry #33's real 2B/2C
  numbers -- a different measure here would mean the thing being applied
  doesn't match the thing that was measured.
- **Threshold**: `ANSWER_NEAR_DUP_THRESHOLD` (0.4, the existing constant)
  as the default, but configurable via `semantic_dedup()`'s `threshold`
  param and `export.py`'s `--semantic-dedup-threshold` CLI flag -- and,
  per the explicit ask not to trust a guessed threshold for an actual
  drop decision, `--semantic-dedup-report` runs identical detection and
  collision logic and writes `semantic_dedup_report.json` (every
  candidate pair, its ratio, and which record would survive) without
  removing anything, independent of whether `--semantic-dedup` itself is
  on. `--semantic-dedup` (the actual drop) defaults to **off** -- the
  report mode exists specifically so the threshold gets tuned against
  the Step 6 re-run's real data before this ever changes a real export's
  row count, not applied blind on the strength of the 0.4 report-only
  calibration alone.
- **Collision rule**: the longer answer survives, not whichever was
  written first. `dedup_by_question` already keeps first-seen, but that
  rule doesn't fit here -- entry #33's manual 2B read found a near-dup
  group where a later-generated pair had consolidated several shorter
  rewordings into the single most complete answer, so "first" is not a
  reliable proxy for "best" for this kind of duplicate. Ties keep the
  lower record index for determinism. A record that loses one comparison
  but would have won another (a mutually-similar cluster of 3+) is still
  dropped -- only the single longest answer in a redundant cluster
  survives, not one survivor per pairwise comparison.

Not run against the real corpus yet -- this step built and tested the
mechanism (`tests/test_export_formats.py::TestSemanticDedup`, `test_export.py`'s
report/applied/off-by-default wiring tests) against synthetic records
only. The Step 6 re-run is where `--semantic-dedup-report` gets pointed
at real data and the 0.4 default gets confirmed or adjusted.

## 2026-08-19 — Phase 3 Step 5: per-host politeness (ROADMAP #9's remaining gap)

### 38. Concurrency cap and Crawl-delay wired through the real crawl_worker call site, not just built in isolation
`crawl/politeness.py::HostPoliteness` is new: a per-host
`asyncio.Semaphore` (default `config.MAX_CONCURRENT_REQUESTS_PER_HOST` =
2) bounding concurrent in-flight requests to one host regardless of
total worker count, plus a per-host minimum spacing between successive
request *starts* (`config.DEFAULT_POLITENESS_DELAY_SECONDS` = 0.5s,
overridden by the site's own `Crawl-delay` when robots.txt specifies
one). The per-host lock guarding the timestamp read/sleep/write is held
only across that, never across the caller's actual fetch — it serializes
request starts for one host without blocking workers assigned to other
hosts or holding a concurrency slot idle longer than the delay itself
requires.

`robots_cache.py::_parse_rules()` now collects `Crawl-delay` per
user-agent group the same way Allow/Disallow already are (RFC 9309 group
selection: exact-agent match wins over `*`, a malformed value is
ignored rather than aborting the whole parse) -- it was previously
dropped entirely, silently, since nothing read it (confirmed in
ROADMAP.md #9's prior text). `HostPolicy` gained a `crawl_delay: float |
None` field, populated in `RobotsCache.get_policy()`.

**Wired through the real call site in the same step it was built, per
the pattern entry #28/#35 established**: `crawl_worker` takes an
optional `politeness: HostPoliteness | None = None` param and wraps only
the `fetch_fn` call in `politeness.hold(row.host, crawl_delay)` --
`nullcontext()` when no instance is configured, same optional-dependency
pattern as `robots_cache`. `main.py` creates exactly **one**
`HostPoliteness` instance and passes the same object to every
`crawl_worker` task it spawns -- per-host state (the semaphore, the last
request timestamp) has to be shared across workers for the throttling to
apply at all; one instance per worker would silently defeat the whole
mechanism; a green test suite wouldn't catch that, only real cross-worker
wiring would.

Confirmed with `--dry-run` against `docs.manim.community` after wiring
(no live crawl -- that's Step 6) that `main.py` still imports and runs
cleanly.

**Testing**: `tests/test_politeness.py` covers `HostPoliteness` in
isolation (spacing enforced, different hosts not cross-throttled,
`crawl_delay` override, concurrency cap holds under real concurrent
tasks, semaphore released even when the caller raises).
`tests/test_crawl_worker.py::test_politeness_spaces_out_successive_fetches_to_the_same_host`
is the integration-level guard -- two seeded URLs on the same host
through the real `crawl_worker`, asserting the measured gap between
`fetch_fn` call timestamps, not just that the unit works standalone.
`tests/test_robots_cache.py` covers `Crawl-delay` parsing (captured,
absent-by-default, scoped to the matched agent group only, malformed
value ignored without breaking the rest of the file's rules).

Not yet exercised against a real site with a real `Crawl-delay` --
none of the 5 fixture sites specify one (still true after this step);
the mechanism is measured to work correctly in isolation and through the
real call site, not proven against a real target's actual `Crawl-delay`
value yet.

## 2026-08-19 — Phase 3 Step 6: re-run against docs.manim.community, before/after all five fixes

### 39. Full before/after: pre-rebuild audit -> Part D baseline -> Phase 3 applied
Identical parameters to Part D (same site, `en/stable/`, blank intent,
max_pages=20, max_depth=2, thresholds=0/0, JSONL+RAG both on, Ollama
cloud deepseek-v4-flash + local nomic-embed-text, 5/2 workers) —
identical is what makes the comparison controlled. Part D's corpus was
archived to `archive/part-d-baseline/` (canonical.jsonl, frontier.db,
chroma_db/, dataset_report.txt — verified byte-identical to the
originals via md5sum before deleting `data/run/`'s contents) before
firing, per the explicit requirement that it's irreplaceable without
another paid run.

Page counts differ (41 vs. 35) — `max_pages` is a fetch budget, not a
hard ceiling (ROADMAP #28, ~confirmed again), and crawl order across
concurrent workers isn't deterministic between runs, so this run
happened to expand more `reference/` pages and never reached the
`tutorials/` branch Part D sampled. Reported per-page, not per-run,
as asked, for exactly this reason.

| Metric | Pre-rebuild audit | Part D baseline | Phase 3 applied |
|---|---|---|---|
| Pages | 30 | 35 | 41 |
| Vectors | 9,204 | 1,635 | 402 |
| **Vectors/page** | **306.8** | **46.7** | **9.8** |
| Q&A pairs | n/a (no per-pair accounting in that pipeline) | 1,144 | 320 |
| **Pairs/page** | n/a | **32.7** | **7.8** |
| Exact-duplicate questions | n/a | 1 | 0 |
| Near-duplicate answer pairs | n/a | 2,396 | 65 |
| Near-dup pairs per row | n/a | 2.09 | 0.20 (10.3x lower) |
| &nbsp;&nbsp;same_chunk | n/a | 380 | 51 |
| &nbsp;&nbsp;adjacent_chunk | n/a | 300 | 9 |
| &nbsp;&nbsp;other | n/a | 1,716 | 5 |
| Avg chunk chars | n/a | 1,527.5 | 1,256.7 |
| Mean link-syntax % of chunk chars | n/a | 45.61% | 0.95% (real links: 0%; remainder is `![image](url)` markdown, deliberately untouched) |
| Chrome phrases found in `source_chunk` | present (motivated the rebuild) | 0 (fixed step 8 Part D) | 0 (12-phrase re-check, real data) |
| Pairs-per-chunk at 4-5 (the old padding band) | n/a | ~98.9% of chunks | 76.9% of chunks (23.1% now land at 1-3) |

**Vectors/page fell 31x from the original pre-rebuild figure (306.8 ->
9.8), 4.8x from the Part D baseline alone** — chrome-stripping (step 8)
and link-syntax normalization (Phase 3 Step 1) account for most of the
character-budget reduction that drives fewer/smaller chunks; the
variable-count prompt (Step 3) and both drop in parallel. **Near-dup
pairs per row fell 10.3x** (2.09 -> 0.20) — bigger than the pairs/page
drop alone (4.2x) would explain by combinatorics, meaning the actual
per-row duplication *rate* dropped, not just the row count. The `other`
near-dup category (non-adjacent same-page pairs) fell hardest in
absolute terms (1,716 -> 5) — consistent with same-chunk padding
(step 3) and link boilerplate (step 1) both having inflated it
indirectly, not just the two categories named after them directly.

**Verified, not assumed**: re-checked all 12 chrome phrases from entry
#32/#35's diagnosis (Light/Dark mode, Skip to content, Back to top, View/Edit
this page, Expand, Menu, Toggle, Copy to clipboard, Copied!) against
every `source_chunk` in the new corpus — zero matches. Re-measured the
10 chunks with the highest remaining "link syntax" fraction (max 11.5%)
and confirmed by inspection every one is `![image](url)` markdown, the
one link form `normalize_link_text()` deliberately leaves alone (an
image's `src` isn't redundant with its alt text the way a hyperlink's
URL is with its visible text).

**Sections/export filenames at depth 1/2/3**: re-derived `section`
offline against the real corpus (matching Phase 2A's method) since
`export.py --section-depth` doesn't actually re-derive anything —
`package_plain_jsonl`'s `section_depth` param is still dead, grouping is
still purely the already-baked `section` field (confirmed again, not
just assumed from the earlier finding). Depth 1: 5 clean categories
(changelog 71, faq 19, installation 29, reference 192, reference_index
5) — this run's page mix didn't include `tutorials/`/`guides/`/`conduct.html`
the way Part D's did, a direct consequence of the different crawl
order noted above. Depth 2 and depth 3 produce **identical** section
sets again (41 sections, same as page count) — this site's structure
still never nests past 2 path segments past the seed prefix. 7 of 41
slugs hit the 60-char cap and got a content-hash suffix at depth 2/3;
**zero required a numeric disambiguation suffix at any depth** — same
finding as Part D, real collisions remain unobserved on this site's
actual URL shapes. `unified.jsonl` row count (316) equals the sum of
`manifest.json`'s per-section `row_count` at all three depths — no row
lost or double-counted by the split.

**Verbatim sample, tutorial-shaped page** (`installation/uv.html`, a
how-to page — this run didn't reach `tutorials/building_blocks.html`,
see above): 20 consecutive pairs read clean end to end — accurate,
grounded, no restatement padding, no chrome, no link-syntax leakage. A
representative pair: *"What is the recommended tool for managing Python
environments when installing Manim?"* -> full, accurate, self-contained
answer citing `uv` and the `pip` fallback.

**Verbatim sample, reference page**
(`reference/manim.mobject.types.point_cloud_mobject.PMobject.html`, 16
pairs, real API content): read clean too, with one real, honestly-noted
observation -- pairs 5-9 (chunk 1, general `Mobject.add_points`/
`reset_points`/`thin_out`/`sort_points` questions) and pairs 10-14
(chunk 2, the same methods framed as `PMobject`-specific) cover
overlapping ground. This is `dataset_report.py`'s `other` category (same
page, non-adjacent chunks) and traces to the *source page itself*
repeating method docs across a summary table and a full autodoc
section, not an extraction artifact -- Sphinx's own structure, not
something the fixes in this phase target or should be expected to
catch. Reported as observed, not smoothed over.

**Not yet applied**: `--semantic-dedup` (Step 4) stayed off for this
export (as designed — off by default). A `--semantic-dedup-report` run
against this real corpus, to see what the 0.4 threshold would flag on
top of the near-dup counts already reported above, is the natural next
check but wasn't run as part of this step's report.

## 2026-08-19 — Two post-Step-6 checks: semantic-dedup threshold reality, and a direct pairs-per-page diff

### 40. --semantic-dedup-report at 0.80-0.95 found nothing; the real range (0.4-0.7) is contaminated by template false positives, including at its own top
Requested thresholds first: 0.80, 0.85, 0.90, 0.95 against the real
Step 6 corpus (320 pairs) all produced **zero candidates**. Swept lower
to find where candidates actually start: 0.7 -> 0, 0.6 -> 7, 0.5 -> 15,
0.4 (the existing calibrated default) -> 47, 0.3 -> 177. Near-verbatim
duplicates (what a 0.8+ ratio requires) essentially don't exist in this
corpus anymore -- consistent with same-chunk padding (2B's dominant
cause) being the thing Step 3's prompt targeted directly.

**Read all 47 candidates at threshold 0.4 with both full pairs, not just
a curated 10** -- and found the working range is not safe to apply
blind. Two representative examples, verbatim, both directions:

*True near-duplicate (correctly caught, survivor is the better pair --
this is the PMobject case specifically asked about):*
> DROPPED (ratio 0.633) Q: What does the `reset_points` method do in Manim? A: The `reset_points` method sets the `points` attribute of the mobject to an empty array.
> SURVIVOR Q: What does the reset_points method do in PMobject? A: The reset_points method sets the 'points' attribute of the PMobject to be an empty array. This effectively clears all the points that were previously added. The method returns Self.

The survivor is genuinely more complete (states the clearing effect and
the return value) -- the longer-answer-wins collision rule worked as
designed here.

*False positive, and it is the single HIGHEST-ratio candidate in the
entire set (0.687, higher than the true positive above):*
> DROPPED Q: What does the SceneInteractRerun object do? A: The `SceneInteractRerun` object is used during scene interaction. When it is encountered in `Scene.interact()`, it triggers a rerun of the scene.
> SURVIVOR Q: What does the SceneInteractContinue object do? A: The `SceneInteractContinue` object is used during scene interaction. When it is encountered in `Scene.interact()`, it triggers the end of the scene interaction and continues with the rest of the animations, if there are any remaining.

These are two different classes with different behavior (rerun vs.
continue) sharing a templated sentence ("X object is used during scene
interaction. When it is encountered in Scene.interact(), it triggers
Y") -- SequenceMatcher scores the shared template higher than the true
positive above scores its shared content. Same pattern recurs
repeatedly in the candidate list: per-OS install instructions
(Windows/macOS LaTeX, Debian/Fedora TeX Live commands), different
animation-module summaries, different mobject attributes (`always` vs
`animate`), different changelog PRs -- all flagged as near-duplicate
purely because Q&A pairs about structurally parallel but factually
distinct facts share boilerplate phrasing. 37 of the 47 candidates are
even *same-chunk* pairs, not cross-chunk -- so this isn't a same-chunk-
is-safe/cross-chunk-is-risky distinction either; a single chunk that
legitimately documents three parallel sub-facts (three OSes, two
similarly-shaped classes) produces exactly this failure mode regardless
of chunk boundaries.

**Recommendation: do not enable `--semantic-dedup` on this corpus at any
single fixed threshold as currently measured.** There is no ratio cutoff
in the observed distribution that cleanly separates true from false
positives -- the highest-ratio candidate in the whole set is a false
positive, so raising the threshold doesn't reliably filter toward safety,
it just removes real duplicates first while leaving some false positives
in reach. This is exactly the finding the `dry_run`/report-mode design
was built to surface before the threshold got trusted blind
(`LESSONS_LEARNED.md` #37's stated purpose) -- it worked as intended.
Not a mechanism bug: `find_near_duplicates()` measures answer-text
similarity, and whole-answer `SequenceMatcher.ratio()` cannot distinguish
"the same fact reworded" from "structurally parallel but distinct
facts" -- that would need a different signal (shared distinctive
content words specifically, not just character-sequence overlap) to
fix, not a different number. Not built here -- this is the measurement,
per the explicit ask.

### 41. Direct pairs-per-page diff: mostly padding removal, but one page shows a real, specific, enumerable loss
Compared the two archived corpora directly rather than trusting the
aggregate 32.7 -> 7.8 pairs/page drop alone. 7 pages exist in both Part
D's baseline and the Step 6 corpus; two read in full.

**`tex_file_writing.html` (21 -> 13 pairs): clean consolidation, no
real loss found.** Every function documented in Part D's 21 pairs
(`compile_tex`, `convert_to_svg`, `delete_nonsvg_files`,
`generate_tex_file`, `insight_inputenc_error`,
`insight_package_not_found_error`, `make_tex_compilation_command`,
`print_all_tex_errors`, `print_tex_error`, `tex_hash`,
`tex_to_svg_file`) still has at least one pair in the new 13. The
difference is pure consolidation: Part D routinely split one function
into 2-3 separate pairs ("what does X do" / "what are X's parameters" /
"what is X's return type"), the new corpus asks for "parameters and
return type" together in one pair. Content-level cause, confirmed by
chunk count: link-syntax removal (Step 1) let the same real content
pack into 3 parent chunks instead of 5, so fewer chunk-boundary-driven
question repeats existed to generate in the first place.

**`VGroup.html` (26 -> 12 pairs): real, specific content loss alongside
the padding removal -- not clean.** Confirmed real padding correctly
removed (Part D had 4 separately-worded pairs all asking "what does the
`add` method do", collapsed to one fuller pair in the new corpus; "what
is VGroup" was asked and answered near-identically twice in Part D,
once in the new corpus) and one Part D pair that was near content-free
to begin with (`_original__init__`'s answer was the literal boilerplate
"initializes self. See help(type(self)) for accurate signature." --
losing this is not a loss). But three facts present in Part D's pairs
are **not recoverable from any pair in the new 12**:
- VGroup's base class (`VMobject`) -- a complete, standalone Part D pair,
  absent entirely from the new corpus.
- The **non-mutating** `+`/`-` operator behavior. The new corpus's
  "combine two VGroups" pair covers `+=`/`-=` (mutates in place) fully
  and correctly, but never states that plain `+`/`-` construct a new
  VGroup *without* modifying the original -- Part D's pair stated both
  halves of the contrast explicitly; the new corpus states only half.
- The attribute list. Part D had one pair enumerating 12 VGroup
  attributes with descriptions where available; the new corpus surfaces
  3 of those 12 (`always`, `animate`, `fill_color`) as individual,
  more-detailed pairs, but 9 (`animation_overrides`, `color`, `depth`,
  `height`, `n_points_per_curve`, `sheen_factor`, `stroke_color`,
  `width`, `target`, `original_id`) appear nowhere in the new corpus.
  Worth qualifying, not overstating: 7 of those 9 had no description at
  all in Part D's own answer (bare attribute names), so their loss is a
  loss of *name presence*, not explained content; `depth`/`height`/
  `width` did have real one-line descriptions ("The depth/height/width
  of the mobject") that are now gone, though each was already close to
  self-evident from the attribute name.
- One code-example walkthrough (`ArcShapeIris`, a list-comprehension
  example building several circles into a VGroup) has no equivalent
  pair in the new corpus at all.

**Conclusion, stated directly per the ask**: the pairs/page drop is
*mostly* redundancy removal working as intended, confirmed by a second,
cleaner page (`tex_file_writing.html`) showing no loss at all -- but
`VGroup.html` proves it is not uniformly lossless, and the honest
answer is "mostly junk, with a real minority of coverage loss on at
least one page," not "confirmed junk-only." Not fixed here -- this was
the direct check the aggregate numbers can't make, per the explicit
ask; whether the loss rate justifies a fix (e.g. a lower per-chunk pair
floor, or explicitly prompting for attribute/enum tables to stay
list-form rather than being split one-per-item) is a decision for
whoever scopes the next round, not made in this measurement.

## 2026-08-19 — VGroup loss diagnosis and a general cross-site link-text-loss check

### 42. Zero real content lost alongside a dropped URL, across all 5 canonical fixture sites
The general question, checked before assuming the manim-specific finding
generalizes: when `normalize_link_text()` drops a link's visible text
along with its URL (not just the URL), is any of that text ever real
content rather than navigation? Ran the real pipeline (`clean_html` ->
`DefaultMarkdownGenerator` -> `normalize_link_text`) against all 5 of
this project's canonical fixture sites (`tests/fixtures/*.rendered.html`
-- blog.cloudflare.com, docs.manim.community, fastapi.tiangolo.com,
stackblitz.com, www.manim.community; the same 5 CLAUDE.md's scope-test
claim refers to), and inspected every case where text was dropped, not
just counted them.

| Site | Total links | Text kept | Text+URL dropped | Dropped chars |
|---|---|---|---|---|
| blog.cloudflare.com | 63 | 60 | 3 | 15 |
| docs.manim.community | 114 | 103 | 11 | 67 |
| fastapi.tiangolo.com | 91 | 60 | 31 | 47 |
| stackblitz.com | 3 | 3 | 0 | 0 |
| www.manim.community | 22 | 22 | 0 | 0 |

Every distinct dropped text across all 5 sites: `"Skip to content"`,
`"Back to top"`, `"View this page"`, `"Edit this page"`, `"¶"` (heading
anchors -- FastAPI's MkDocs Material theme puts one after every single
heading, accounting for most of its 31), and one literal empty string
on Cloudflare -- traced to `[](https://blog.cloudflare.com/)`, a bare
logo/home link with no alt text, confirmed nothing to lose. **No
genuine content -- no fact, no name, no data point -- was found dropped
alongside a URL on any of the 5 sites.** The mechanism is safe as
measured; not fixed because nothing here needed fixing. If a future
site's theme phrases its skip-link/back-to-top copy differently and it
turns out NOT generic (an edge case not observed on these 5), the fix is
extending `DEFAULT_GENERIC_LINK_TEXT`, per its own docstring -- not
reconsidering the drop-vs-keep decision itself.

### 43. The VGroup losses are unextracted, not absent -- all three facts survive fully intact in the actual chunk sent to the LLM
Diagnosed each of entry #41's three "lost" VGroup facts individually,
per the explicit ask (present-but-unextracted and absent-from-chunk need
opposite fixes). Checked the real, already-persisted `source_chunk`
text from the Step 6 corpus directly -- no new fetch needed, this is
exactly what extraction was given:

- **Base class (`VMobject`)**: chunk 0 contains, verbatim, "Bases:
  `VMobject`" -- a standalone, cleanly formatted line, immediately after
  the class signature.
- **Non-mutating `+`/`-` vs. mutating `+=`/`-=`**: chunk 0 contains the
  full doctest session showing both, including the code comments
  spelling out the exact contrast: `>>> vg + square  # a new VGroup is
  constructed` immediately followed by `>>> vg  # not modified`, then
  `>>> vg += square` followed by `>>> vg  # modifies vg`.
- **The 9 "missing" attributes**: chunk 1 contains the complete
  Markdown attribute table, all 12 rows, including the 7 rows whose
  description cell is blank in the source page itself (`animation_overrides`,
  `color`, `n_points_per_curve`, `sheen_factor`, `stroke_color`,
  `target`, `original_id`) -- unmangled, not truncated, not merged into
  another row.

**All three facts were fully present, cleanly formatted, in the exact
text the LLM was given -- this rules out the content pipeline (chrome-
stripping, link normalization, chunking) as the cause.** Combined with
entry #42's finding, this means Phase 3 Steps 1/2 did not cause any of
this page's content loss. The cause is upstream of content prep,
inside extraction itself: the model had everything and chose not to
write pairs about the base-class fact, the non-mutating-operator half of
the contrast, or 9 of the 12 attribute rows. Recorded as `ROADMAP.md`
#32, a working hypothesis (Step 3's anti-padding instruction biasing
pair selection away from terse/tabular facts toward richer prose facts)
rather than a confirmed cause -- only one page examined at this depth,
not yet checked against other tabular reference pages. Not fixed here,
per the explicit ask to diagnose before proposing anything.

## 2026-08-19 — Real hang found and fixed resuming a completed crawl

### 44. Quiescence was only reachable from a decrement -- any already-final startup state hung forever
Found live, not in a test: resuming the FastAPI crawl (reference+tutorial
branches, max_pages=20) after it had already reached 34 done pages
(past the cap) with nothing left `in_progress` -- the process never
exited. Every crawl worker just looped `claim() -> None -> sleep ->
repeat` forever, and had to be killed externally three times before the
cause was found.

**Root cause**: `Frontier._locked_claim()` returns `None` directly when
the cap blocks a claim, with no call to `_locked_check_quiescence()`.
That check is *only* ever invoked from the handful of methods that
decrement `_in_flight` -- a successful `claim()`, `content_done()`,
`results_done()`/`put_results` resolving. Every one of those requires
something to still be in flight in *this process* (`_in_flight` is
process-lifetime-only, starts at 0 on every fresh `Frontier()`
regardless of what a prior process left behind). A process that starts
in an already-final state -- an empty frontier, one where every row is
already terminal, or one where `max_pages` was already met with nothing
left `in_progress` to recover -- has no decrement to ever reach the
check from, so `quiescent` never gets set. **Same shape as the
cascade-termination bug this architecture's global-quiescence design
was built to avoid** (`tests/test_frontier_quiescence.py`'s docstring):
a completion-triggered check in a state where nothing completes. The
cap-blocked `claim()` case is one instance of the general problem, not
the whole problem -- fixing only that branch would have left the empty-
and all-terminal-frontier cases still broken.

**Fix**: `Frontier.recover_crashed()` -- the real startup call site,
called by `main.py` right after `frontier.open()` and before any worker
task exists -- now calls `_locked_check_quiescence()` unconditionally
as its last step, inside the same lock it already holds. Safe in every
case: if real work remains (queued rows, cap not met), the check is a
no-op and normal reactive triggering takes over as before; if the
frontier is already final, quiescent gets set immediately instead of
never.

**Confirmed as a real regression, not assumed**: `git stash`-reverted
the fix and re-ran the new tests -- they hung and the run had to be
killed after the 30s tool timeout (exit 143), the same symptom as the
live crawl. `tests/test_frontier_startup_quiescence.py` covers all
three already-final startup states named above, each against a real
on-disk db file reopened by a fresh `Frontier` instance (not `:memory:`,
which can't simulate "a prior process already wrote this state and
exited" -- a fresh `:memory:` connection is always a blank database), plus
one test running a real `crawl_worker`-shaped loop end to end to prove
workers actually exit, not just that the `Event` gets set.

**The one-row crash-window gap, checked for real rather than assumed
handled**: the FastAPI run's own crash window left exactly one page
(`tutorial/dependencies/classes-as-dependencies`) with 32 real,
correctly-written Q&A pairs in `canonical.jsonl` but its frontier row
never reached `done` -- `max_pages` being already exceeded means it will
never be reclaimed even after the fix (correct: this is the pre-existing
"queued rows left live by design" behavior applying uniformly to a
crash-recovery requeue too, not a new gap). Since the cap prevented a
live second extraction attempt for that URL in this run, the suppression
path was verified directly instead: loaded the real `canonical.jsonl`
into a fresh `Writer` (exactly what a resumed process's preload does),
confirmed `already_written()` returns `True` for that exact URL, then
called `write()` again with distinct synthetic content -- the file's
line count didn't change (2570 before, 2570 after) and the synthetic
content never appeared. `already_written()` genuinely suppresses a
second append; it doesn't just happen to be present and unconsulted.
(`tests/test_writer.py::test_already_written_url_skips_jsonl_append`
already covered this at the unit level and was passing throughout --
this was the same mechanism confirmed against this run's real data, not
a new finding.)

## 2026-08-19 — Second-site crawl: fastapi.tiangolo.com (MkDocs Material + mkdocstrings)

### 45. Full metrics on a second, unrelated generator -- and ROADMAP #32 confirmed, not just replicated by coincidence
Every number this project had before this run came from one site
(docs.manim.community, Sphinx + Furo). Crawled fastapi.tiangolo.com
(MkDocs Material theme, mkdocstrings-generated reference pages --
shares no code or theme with manim's stack) with parameters identical
to the manim runs except branch selection: `reference` + `tutorial`
specifically, not `all` -- FastAPI's landing page links to ~50 extra
single-page branches (13 translated-homepage variants, ~38 external
sponsor/social domains) that manim's docs root doesn't have, which
would have diluted representation of the two content shapes #32 needed
compared on the same site. Confirmed via `--dry-run` before firing that
both branches are depth-1 reachable (24 and 51 URLs respectively),
so `max_depth=2` needed no change. **This is a deliberate deviation
from strict branch-selection parity with the manim protocol, flagged
per the explicit ask** -- max_pages/max_depth/thresholds/intent/
outputs/model/workers are all identical.

| Metric | manim (Step 6) | FastAPI | Note |
|---|---|---|---|
| Pages | 41 | 35 | |
| Pairs | 320 | 2,570 | |
| **Pairs/page (mean)** | 7.8 | 73.4 (max 643 on one page) | content shape, see below |
| Vectors | 402 | 2,032 | |
| **Vectors/page** | 9.8 | 58.1 | tracks pairs/page |
| Chunks | 78 | 600 | |
| Chunks/page | 1.9 | 17.1 | tracks pairs/page |
| **Pairs/chunk (mean)** | 4.1 | 4.28 | consistent -- see below |
| Near-dup answer pairs (raw) | 65 | 13,681 | scaling artifact, see below |
| **Near-dup rate, normalized (% of possible same-page pairs)** | 3.40% | 3.03% | near-identical |
| Mean link-syntax % of chunk chars | 0.95% | 0.09% | FastAPI is cleaner |
| Chrome phrases found in `source_chunk` | 0 | 0 | zero MkDocs-specific tuning needed |
| Sections at depth 1 | 5 | 32 | naming-convention shape, see below |
| Sections at depth 2/3 | 41 / 41 | 35 / 35 | both flatten past depth 1-2 |
| Filename collisions (any depth) | 0 | 0 | |
| Hash-capped filenames (60-char) | 7 of 41 | 0 of 35 | naming-convention shape |

**Every substantial difference traced to content shape, not a pipeline
problem** -- checked each one, not assumed:
- **Pairs/page, vectors/page, chunks/page (~7-9x higher)**: mkdocstrings
  auto-generates one page per class/module documenting *every* method
  with full signatures -- `reference/fastapi` (the `FastAPI` class
  itself: `.get()`, `.post()`, `.websocket()`, `.middleware()`, dozens
  of decorator methods, each with several parameters) is 150 chunks
  alone. Confirmed genuine page size, not an extraction anomaly, by
  checking **pairs/chunk stays constant** (4.3 for `reference/fastapi`,
  4.4 for `reference/apirouter` and `reference/parameters`, 5.0 for a
  small tutorial page) -- the page is just structurally larger, chunk
  count scales with it, pairs/chunk doesn't move.
- **Raw near-dup pair count (65 -> 13,681) looks alarming, isn't**: raw
  near-dup pair count scales roughly with C(n,2) per page, so a site
  with much larger pages produces a much larger raw count at an
  *unchanged* per-item duplication rate. Normalized against the actual
  number of possible same-page pairs (`sum(n*(n-1)/2)` per page), the
  two sites are within half a point of each other (3.40% vs. 3.03%) --
  the raw counts alone would have been a misleading comparison.
- **Link-syntax overhead lower on FastAPI (0.09% vs. 0.95%)**: hand-
  written MkDocs prose uses fewer inline cross-reference links than
  Sphinx's heavily-cross-referenced autodoc output. The 3 chunks with
  >5% remaining are all confirmed `![image](url)` markdown (screenshots
  in the tutorial), same pattern as manim, correctly left alone by
  design.
- **Zero chrome leaks, with zero MkDocs-Material-specific patterns ever
  added** to `DEFAULT_TEXT_PATTERNS`/`DEFAULT_EXCLUDED_SELECTOR`/
  `DEFAULT_GENERIC_LINK_TEXT` -- every one of those lists was tuned
  entirely against Furo. One coincidental substring match (`"navigation"`
  in a sentence about VS Code's CodeLens navigation feature) checked by
  hand and confirmed real content, not chrome.
- **Sections much more granular at depth 1 (32 vs. 5)**: naming
  convention, not depth. Most FastAPI reference/tutorial pages are a
  single path segment past the branch prefix (`/reference/fastapi`,
  `/tutorial/first-steps`) where manim's Sphinx module paths nest
  several dotted segments deep, so depth 1 already gives near-per-page
  granularity here versus needing depth 2 on manim.
- **Zero filenames hit the 60-char cap**: FastAPI's section names are
  short single/hyphenated words (`apirouter`, `parameters`); manim's are
  full dotted Python module paths
  (`manim.mobject.geometry.arc.AnnularSector.html`). Same disambiguation
  mechanism, different naming convention feeding it.

**ROADMAP #32, folded in as asked**: split pairs/chunk by branch
(`reference/*` = tabular/mkdocstrings-generated, `tutorial/*` = prose)
-- 4.30 mean vs. 4.13 mean, no systematic aggregate difference, matching
manim's own pairs-per-chunk consistency across content types. But a
row-level audit of `reference/parameters` chunk 1 (a real parameter
table, 10 distinct rows: `default`, `default_factory`, `alias`,
`alias_priority`, `validation_alias`, `serialization_alias`, `title`,
`description`, `gt`, plus one row split across the chunk boundary)
against its 5 generated pairs found **3 rows -- `alias_priority`,
`title`, `description` -- mentioned in neither the question nor the
answer of any pair**, checked against full answer text, not just
question titles. `alias_priority` has a real, substantive description
in the source ("Priority of the alias. This affects whether an alias
generator is used") -- a genuine loss, not a thin/self-evident one.
This is the same shape as VGroup's loss on manim, on a generator that
shares no code or theme with Sphinx/Furo. **Confirms #32 as a real
prompt-behavior issue, not a one-page manim artifact** -- upgraded from
working hypothesis to confirmed in ROADMAP.md, with a fix now worth
choosing rather than measuring further first.

**Verbatim samples read clean on both content shapes**, no chrome, no
link-syntax leakage, in every pair sampled (18 from `reference/parameters`,
18 from `tutorial/first-steps`) -- the #32 gap is a coverage question
(which facts get a pair), not a quality question (whether the pairs
that do get generated are accurate).

## 2026-08-19 — ROADMAP #32 fix attempt: characterized, tested, not applied

### 46. Table-row coverage is a real, sizable gap -- and the fix that closes it exists, but wasn't applied pending review
**Characterization first, offline, against both archived corpora** (no
crawl, no new fetches): identified every markdown table row shaped like
`` | `name` | description | `` across every chunk in both
`archive/step6-manim-baseline/` and `archive/fastapi-baseline/` --
155 table-shaped chunks, 997 rows -- and checked whether each row's name
appears in any of that chunk's actual generated pairs.

| | Count | Covered |
|---|---|---|
| Bare rows (description <10 chars, often empty) | 103 | 42.7% |
| **Rich rows (real description present)** | **894** | **81.9%** |

Per site, not pooled (manim's misses are disproportionately *inherited*
attributes -- `always`/`depth`/`height`/`width` -- repeated near-
identically across dozens of subclass pages, which is arguably correct
to skip on the 40th repetition; FastAPI's misses are page-specific,
real losses):
- manim rich-row coverage: **59.8%**
- FastAPI rich-row coverage: **87.8%**

18.1% of rich rows overall (162 rows, 75 distinct names -- not a
handful of repeated boilerplate) get zero mention in either the
question or answer of any pair for that chunk. Real gap, not "nearly
correct" -- matches the explicit ask's framing.

**A separate, unrequested finding surfaced during characterization,
worth recording on its own**: baseline's own behavior on the manim
table chunk (this specific 12-attribute VGroup table, one live sample)
included a "list all attributes" summary pair that **fabricated
descriptions for bare/undescribed rows** -- e.g. stating `color` "holds
the color of the mobject" and `n_points_per_curve` "relates to curve
point density," neither of which the source table says (both cells are
blank in the real page). This directly violates the prompt's own
existing rule ("derived ONLY from the provided text"). Not something
either prompt variant below does -- both leave bare rows alone entirely
rather than inventing content for them -- but worth flagging as an
existing failure mode independent of the coverage question, found
because this test happened to sample it.

**Prompt variants tested**: 3 prompts (baseline unchanged; variant A --
coverage rule scoped specifically to "a table, list, or enumeration of
distinct named items," anti-padding language otherwise unchanged;
variant B -- an explicit two-step "enumerate every distinct item, then
one pair per item" process) x 4 chunks (manim table/prose, FastAPI
table/prose, exact chunks already analyzed, zero new fetches) = 12
Ollama cloud deepseek-v4-flash calls, confirmed with the user before
firing.

**A methodology note that matters for reading the numbers below**: the
automated same-chunk redundancy measure (`SequenceMatcher.ratio()` on
normalized answer text, the same measure `semantic_dedup()`/
`dataset_report.py` use) flags **92-100% "redundant"** on both variants'
table-chunk output -- and a manual read of every flagged pair confirms
this is a false positive, the identical template-similarity failure
mode already documented in entry #40 ("What does the `depth` attribute
represent?" / "The `depth` attribute represents the depth of the
mobject." shares near-total sentence-template overlap with the
`height` pair despite being a completely different, correctly-covered
fact). The automated measure is not fit for judging redundancy on
output shaped like "many short, structurally parallel, genuinely
distinct one-liners" -- manual verification is the reliable signal here,
consistent with #40's conclusion that this measure needs a different
similarity signal to be trustworthy. Numbers below are the corrected,
manually-verified read, not the raw automated flag.

**Results, per site, not pooled** (rich-row coverage / genuine bare-row
coverage / redundancy after manual correction):

| | manim table (VGroup, 7 bare + 7 rich rows) | FastAPI table (parameters, 9 rich rows, 0 bare) |
|---|---|---|
| Baseline | 3 pairs. Rich 7/7 (100%, via one dump-style pair -- see fabrication finding above). Bare 6/7 "covered" but **fabricated**, not real. | 5 pairs. Rich 6/9 (67%) -- misses `alias_priority`, `title`, `description`, matching entry #45's original finding exactly. |
| **Variant A** | 8 pairs, one per method/attribute. Rich **7/7 (100%)**. Bare **0/7 (0%, correctly and honestly skipped, no fabrication)**. `add` method stayed one consolidated pair (not fragmented). Manually confirmed non-redundant. | 9 pairs, one per parameter. Rich **9/9 (100%)** -- `alias_priority`/`title`/`description` now covered, accurately, each its own clean pair. Manually confirmed non-redundant. |
| Variant B | 13 pairs. Rich 7/7 (100%). Bare 0/7 (0%; automated check initially showed 1/7 for `color`, traced to a false match on the word "color" inside the *`fill_color`* answer's own prose -- corrected by hand, a real limitation of plain word-boundary name matching worth noting for reuse). **But the `add` method got split into 4 separate pairs** (what it does / return type / error / parameters) where baseline and variant A used one -- a real, partial reversion toward the original padding pattern, scoped to multi-aspect methods rather than attribute tables. | 9 pairs, one per parameter. Rich **9/9 (100%)**. Same clean result as variant A -- no over-fragmentation on this chunk (it has no multi-aspect method to over-split, only flat parameter rows). |

**Prose chunks -- this is where variant B actually fails**: manim
prose pairs went 5 (baseline) -> 7 (variant A, genuinely distinct new
facts on manual read: per-OS install commands split correctly,
first-step, troubleshooting) -> 8 (variant B). FastAPI prose pairs went
5 (baseline) -> **5 (variant A, unchanged)** -> **12 (variant B)**.
Read variant B's 12 fastapi-prose pairs in full: real, confirmed
fragmentation of one terminal-log block into thin individual facts
("What command should I use to stop the FastAPI development server?" /
"Press CTRL+C to quit the server." as its own standalone pair,
alongside separate pairs for "what processes are started" and "what
happens during startup") -- this is genuine padding, not a metric false
positive, confirmed by direct read exactly as entry #40's methodology
established: verify before trusting a number either direction.

**Conclusion, per the explicit "don't apply until reviewed" instruction
-- reported, not applied**: **Variant A closes the coverage gap
(100% rich-row coverage on both sites, matching variant B) without
variant B's regressions** (no bare-row padding on either, no method
over-fragmentation on manim, no prose fragmentation on FastAPI --
5 pairs unchanged from baseline). Variant B achieves the same coverage
number but at a real, confirmed redundancy cost in two different
places. This is not a case of "no variant improves coverage without
cost" -- variant A is a genuine, verified win on the evidence gathered.
**Not applied to `content/extraction.py` -- awaiting review of these
numbers**, per the explicit instruction not to apply anything until
they've been seen.

## 2026-08-19 — Variant A applied; fabrication finding checked against real data

### 47. ROADMAP #32 fix applied; the fabrication finding is real but small in the existing corpora, not the 52 it first looked like
Applied Variant A (entry #46) to
`content/extraction.py::QA_EXTRACTION_SYSTEM_PROMPT` -- verified win on
both sites, Variant B's fragmentation cost confirmed by reading, not
just by the (unreliable, see below) metric. Full test suite green,
unaffected (no test asserts the prompt's exact wording).

**Re-verified Variant A doesn't fabricate, on the exact chunk that has
blank cells**: none of the 7 blank-description VGroup attribute rows
(`animation_overrides`, `color`, `n_points_per_curve`, `sheen_factor`,
`stroke_color`, `target`, `original_id`) appear anywhere in Variant A's
8-pair output for that chunk -- checked directly, not inferred from the
0% bare-row-coverage statistic alone.

**Checked the archived corpora for existing fabrication, and the honest
count is much smaller than the raw hit count suggested.** 103 blank-
cell rows exist across the archived manim corpus (FastAPI's has zero --
every one of its table cells has real text). 52 pair-instances mention
a blank-cell row's name at all. Read every one, not just counted word-
matches, because the raw count conflates several different things:
- **31+ are honest name-only enumeration** -- several pairs literally
  say `` `animation_overrides` (no description) `` rather than
  inventing one. Not fabrication; if anything, transparent about the
  gap.
- **A handful are false positives of plain word-matching** -- e.g. a
  `fill_color` explanation using the ordinary English phrase "fill
  color" in prose, matching against the unrelated blank `color`
  attribute row; a code example containing `color=RED` as a Python
  kwarg matching the same way. Not about the row at all.
- **A handful state a specific claim that turns out to be genuinely
  grounded elsewhere in the same chunk** -- confirmed by reading the
  full chunk, not just the flagged row: `CounterclockwiseTransform`'s
  attribute table leaves `path_arc`'s description blank, but its
  `_original__init__` signature elsewhere in the *same* chunk literally
  reads `path_arc =3.141592653589793`, and the pair's claimed default
  matches that number exactly. Sourced from a different part of the
  chunk than the blank cell, not invented.
- **~4 distinct pairs (touching ~9 row-instances) remain genuinely
  ungrounded** -- plausible-sounding claims about standard, predictable
  Animation-class parameters (`run_time`, `path_func`, a second,
  vaguer `path_arc` claim) with no explicit textual basis found
  anywhere in their chunk. Soft violations of "derived ONLY from the
  provided text" -- none verifiably *false* (these are standard enough
  Manim conventions that the claims are plausibly accurate), none as
  clearly invented as the controlled test's `n_points_per_curve`
  example, but not clean either.

**Net finding, stated precisely per the ask**: the failure mode is
real and demonstrated (the controlled test's `n_points_per_curve`/
`color` example is unambiguous fabrication with zero textual basis),
but its footprint in the actual archived production data is small --
roughly 4 pairs, not 52, and none of those 4 are clearly false, just
under-grounded. Recorded as ROADMAP #33, sized XS, not treated as an
urgent existing-data cleanup. Variant A checked clean against the exact
chunk that produced the controlled-test fabrication, so this shouldn't
recur going forward.

**The redundancy-similarity measure has now produced a false positive
twice, in two different call sites, from the same root cause** -- worth
its own note since it's the same measure a real feature depends on.
Entry #40: `semantic_dedup()`'s near-dup detector ranked a false
positive (two different Scene classes sharing a templated sentence)
above a confirmed true positive, which is why pair-level semantic dedup
stays off (ROADMAP #23). This entry: the same `SequenceMatcher`-on-
answer-text approach, applied ad hoc to check the prompt variants
above for redundancy, flagged 92-100% of clean, non-redundant,
one-fact-per-pair table-coverage output as "redundant" -- confirmed by
manually reading every flagged pair. Both failures have the identical
shape: short, structurally-parallel-but-factually-distinct sentences
("the `depth` attribute represents the depth of the mobject" / "the
`height` attribute represents the height of the mobject") score high
on character-sequence similarity despite describing different facts.
Two independent occurrences is a stronger signal than one that this
specific measure -- not just this specific application of it -- doesn't
belong anywhere in this codebase's redundancy judgments without a
different similarity signal underneath it. See ROADMAP #23's note
cross-referencing this entry, so either one found first leads to the
other.

---

<!-- Append new entries below this line, most recent last, dated. -->
