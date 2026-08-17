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

<!-- Append new entries below this line, most recent last, dated. -->
