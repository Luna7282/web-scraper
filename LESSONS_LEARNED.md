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

<!-- Append new entries below this line, most recent last, dated. -->
