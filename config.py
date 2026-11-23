import importlib.util
import os
from enum import Enum
from pydantic import BaseModel
from dotenv import load_dotenv

# SCRAPER_LOAD_DOTENV=0 (Brief 3A, approved in Step 0): don't read
# web-scraper/.env at all -- a launcher (foundry) passes everything the run
# needs itself, settings in the v1 config and the key via api_key_env. Any
# other value, or unset: load .env as always (it never overrides a variable
# that is already set).
if os.getenv("SCRAPER_LOAD_DOTENV", "").strip() != "0":
    load_dotenv()

# Frontier / crawl-pipeline knobs (frontier.py).
MAX_RETRIES = 3
FOLLOW_GATE_EXEMPT_DEPTH = 0  # seed's own children always promote regardless of score

# Per-host politeness (crawl/politeness.py, ROADMAP.md #9). A capped
# small-page run against one host never surfaces the lack of this; a
# larger run against a single host would. MAX_CONCURRENT_REQUESTS_PER_HOST
# caps concurrent in-flight requests regardless of total worker count;
# DEFAULT_POLITENESS_DELAY_SECONDS is the minimum spacing between
# successive requests to a host that specifies no Crawl-delay of its own
# in robots.txt (a site's own Crawl-delay always overrides this default).
MAX_CONCURRENT_REQUESTS_PER_HOST = 2
DEFAULT_POLITENESS_DELAY_SECONDS = 0.5

# Parent/child chunking (chunk_store.py). Values are the same ones the old
# pipeline used via library defaults -- made explicit here, not retuned.
# CHILD_CHUNK_OVERLAP == half of CHILD_CHUNK_SIZE is the measured, documented
# cause of 9,204 vectors from only 30 pages in the original audit
# (LESSONS_LEARNED.md #19) -- whether to change it is a separate decision
# from making it visible, and isn't made in the same change as this comment.
PARENT_CHUNK_SIZE = 2000
PARENT_CHUNK_OVERLAP = 200
CHILD_CHUNK_SIZE = 400
CHILD_CHUNK_OVERLAP = 200

# Chroma (storage/chunk_store.py). Lives under data/run/ alongside
# frontier.db/canonical.jsonl -- all three are run state from the same
# crawl, never a deliverable (see CLAUDE.md's output layout, step 8
# Phase 1D). Not data/run/chroma_db directly: chromadb.PersistentClient
# writes many files at this path, so it gets its own subdirectory rather
# than mixing with frontier.db/canonical.jsonl's loose files.
CHROMA_PERSIST_DIR = "./data/run/chroma_db"
CHROMA_COLLECTION_NAME = "scraper_docs"

class ExtractionStrategy(str, Enum):
    FIRST_N_CHARS = "first_n_chars"           # one call, content[:MAX_EXTRACT_CHARS] -- cheapest, but only ever sees a page's opening slice
    PER_CHUNK = "per_chunk"                    # one call per parent chunk -- complete, N calls/page
    TOP_K_CHUNKS_BY_RELEVANCE = "top_k_chunks_by_relevance"  # chunk, embed, keep the K most intent-relevant -- what makes a large intent-gated crawl affordable

# per_chunk is the default: first_n_chars silently drops everything past
# ~16% of a real reference page (LESSONS_LEARNED.md #25/ROADMAP.md #21 --
# the FastAPI "sponsors" finding). top_k is cheaper but needs an intent to
# rank chunks against; with no intent it falls back to per_chunk (see
# extraction_units.py) rather than picking arbitrarily.
EXTRACTION_STRATEGY = ExtractionStrategy.PER_CHUNK
EXTRACTION_TOP_K = 3
MAX_EXTRACT_CHARS = 4000  # same conservative bound as relevance.py's MAX_EMBED_CHARS -- one real per_chunk unit (2000 chars) is already well under this

class ExtractionPromptVariant(str, Enum):
    STANDARD = "standard"          # QA_EXTRACTION_SYSTEM_PROMPT -- prose Q&A over any content
    CODE_FOCUSED = "code_focused"  # CODE_QA_EXTRACTION_SYSTEM_PROMPT -- instruction->code pairs for runnable examples, prose Q&A fallback otherwise

# Kept at STANDARD -- switch per-run once the code-focused variant has been
# reviewed against real chunks (see LESSONS_LEARNED.md), not as this file's
# default.
EXTRACTION_PROMPT_VARIANT = ExtractionPromptVariant.STANDARD

# Opt-in timing log (crawl/timing.py). Off by default -- when off, no
# TimingLog is built and the workers run exactly the pre-timing code path.
# When on, main()/headless_crawl() append one JSON line per event (fetch,
# relevance scoring, each extraction LLM call, each quality-scoring call,
# per-page summary, run start/end) to <run_dir>/EXTRACT_TIMING_LOG_FILENAME
# (data/run/timings.jsonl for the interactive CLI). Appends rather than
# truncates so a resumed run keeps its earlier events; split runs on the
# run_start event. Env-overridable (same pattern as USE_SYSTEM_ONE below)
# so a measurement run doesn't need a source edit.
EXTRACT_TIMING_LOG = os.getenv("EXTRACT_TIMING_LOG", "false").strip().lower() == "true"
EXTRACT_TIMING_LOG_FILENAME = "timings.jsonl"

# How many of ONE page's extraction units (and, with sample-quality scoring
# on, its pair-scoring calls) extract_worker keeps in flight at once, under
# one asyncio.Semaphore (crawl/pipeline.py). 1 = the original strictly
# sequential per-unit loop, unchanged. Worker count (extract_workers) only
# parallelises across pages; this parallelises within one, which is what a
# single huge page (one 317-unit page took 83% of the Brief 1 Step 2
# baseline, LESSONS_LEARNED.md #82) needs. Each in-flight call occupies one
# asyncio.to_thread slot (extract_fn runs llm.invoke in a thread), so
# extract_workers x this must stay well under the default thread pool
# (min(32, cpu_count + 4)); it also multiplies the request rate the provider
# sees -- see ROADMAP.md #51/#54 for what a provider rate limit does to a run
# today. Env-overridable like EXTRACT_TIMING_LOG.
EXTRACT_UNIT_CONCURRENCY = int(os.getenv("EXTRACT_UNIT_CONCURRENCY", "1"))

# Upper bound on a rate-limit backoff (crawl/pipeline.py::_rate_limit_backoff).
# A 429's own retry hint (Retry-After / Google retryDelay) is used when the
# provider sends one -- ROADMAP.md #51 -- but a per-day quota can say "retry
# in 20 hours", and a queued row parked that far out keeps the run from ever
# reaching quiescence. Longer outages are the circuit breaker's job, not a
# single row's backoff.
RATE_LIMIT_MAX_BACKOFF_SECONDS = 300.0

# Headless contract v1 graceful stop (Brief 3A Step 3): after SIGTERM/SIGINT,
# how long in-flight pages get to finish and be written before the workers
# are cancelled and still-claimed rows are put back to 'queued'. In-flight
# LLM calls can't be cancelled (ROADMAP #58), so this is their window to
# finish. v1 setting "stop_grace_seconds"; legacy runs install no handlers.
STOP_GRACE_SECONDS = int(os.getenv("SCRAPER_STOP_GRACE_SECONDS", "30"))
# Brief 3A Step 4 (ROADMAP #51/#52). Off here = today's behaviour; headless
# contract v1 turns both on by default (headless.SETTINGS).
# SITE_RETRY_BACKOFF: a site's 429/503 re-queues the page with a backoff (the
# site's Retry-After, else 5s doubling per retry, capped at
# RATE_LIMIT_MAX_BACKOFF_SECONDS) instead of an immediate re-fetch.
SITE_RETRY_BACKOFF = os.getenv("SCRAPER_SITE_RETRY_BACKOFF", "false").strip().lower() == "true"
# LLM_UNAVAILABLE_MAX_RELEASES: > 0 = a transient provider error (HTTP
# 500/502/503/504) on extraction releases the page with a backoff WITHOUT
# using a retry, at most this many times per page per run; the next one
# fails the page with a clear reason. 0 = today's path (a generic error,
# counted retry, no backoff).
LLM_UNAVAILABLE_MAX_RELEASES = int(os.getenv("SCRAPER_LLM_UNAVAILABLE_MAX_RELEASES", "0"))

# Brief 3B Step 2 (contract v2 only; headless.SETTINGS_V2_ONLY overrides per
# run). Before any page work: one tiny provider call (a retired model or a
# rejected key fails the run with exit 2), and one embedding when the run
# needs embeddings (intent, RAG, top_k_chunks_by_relevance).
PROVIDER_PREFLIGHT = os.getenv("SCRAPER_PROVIDER_PREFLIGHT", "true").strip().lower() == "true"
EMBEDDINGS_PREFLIGHT = os.getenv("SCRAPER_EMBEDDINGS_PREFLIGHT", "true").strip().lower() == "true"
PREFLIGHT_MAX_TOKENS = 16
# Dry-pass token estimate (estimate.json). Basis, measured: the Brief 2c real
# run (fastapi, 3 pages, 52 extraction calls, every call reporting usage,
# ollama_cloud deepseek-v4.1-flash:cloud, qa prompt): 109,992 input chars
# (system prompt + prefix + unit) = 26,097 input tokens -> 4.215 chars/token;
# 63,949 output tokens = 2.4504 x input. One run, one model, one prompt.
ESTIMATE_CHARS_PER_INPUT_TOKEN = 4.215
ESTIMATE_OUTPUT_PER_INPUT_TOKEN = 2.4504
ESTIMATE_BASIS = ("Brief 2c real run 2026-11-23: fastapi.tiangolo.com, 3 pages, 52 calls, "
                  "ollama_cloud deepseek-v4.1-flash:cloud, qa prompt; 4.215 input chars/token, "
                  "output = 2.4504 x input tokens. One run, one model, one prompt.")

# Brief 3B Step 3: source balancing (contract v2 only, content/balance.py;
# headless.SETTINGS_V2_ONLY overrides per run, and its defaults are what a v2
# run gets -- these module values are read only by v2). Caps are per record
# type. None = no cap. Keep rule: round-robin across a page's units.
MAX_ROWS_PER_PAGE = None
MAX_ROWS_PER_SOURCE = None
LOW_VALUE_POLICY = "keep"         # "skip" | "cap" | "keep"
LOW_VALUE_MAX_ROWS = None
# Low-value page heuristic (content/balance.py::detect_low_value). Segments
# match one whole URL path segment (extension dropped); titles are regexes
# against the lowercased page title.
LOW_VALUE_CHANGELOG_SEGMENTS = frozenset({
    "changelog", "changelogs", "change-log", "changes", "release-notes", "releasenotes", "release_notes",
    "releases", "release-history", "whatsnew", "whats-new", "what-s-new", "news", "history"})
LOW_VALUE_INDEX_SEGMENTS = frozenset({
    "genindex", "py-modindex", "modindex", "tags", "tag", "categories", "category", "archive", "archives",
    "sitemap", "search", "authors"})
LOW_VALUE_CHANGELOG_TITLES = (r"\brelease notes?\b", r"\bchange ?log\b", r"\bwhat'?s new\b", r"\brelease history\b")
LOW_VALUE_INDEX_TITLES = (r"^index\b", r"\bmodule index\b", r"\bsite ?map\b", r"\barchives?\b", r"^tags?\b")
LOW_VALUE_MIN_VERSION_HEADINGS = 3
LOW_VALUE_MIN_LINK_LINES = 20
LOW_VALUE_LINK_LINE_SHARE = 0.5

# Brief 3B Step 4: code_example@1 (generators/code_example.py) sends up to
# this many characters of the PRECEDING unit as context: the chunker often
# puts a code block alone in a unit and its explanation in the one before.
CODE_EXAMPLE_CONTEXT_CHARS = 1500

# Brief 3B Step 6 run safety (contract v2 only; headless settings override
# per run). MIN_CRAWL_DELAY_SECONDS: a floor under the per-host spacing --
# max(site's robots.txt Crawl-delay or DEFAULT_POLITENESS_DELAY_SECONDS,
# this). None = no floor. STOP_ON_SITE_429: the first 429 from the site
# (a page or robots.txt) stops the run (stopped_early, resumable) instead of
# re-queueing the page.
MIN_CRAWL_DELAY_SECONDS = None
STOP_ON_SITE_429 = False

# Brief 3B Step 5: verifiers (contract v2, verifiers/).
VERIFY_MIN_ANSWER_LENGTH = 20            # schema@1 on qa rows: export_formats' default minimum
NEAR_DUPLICATE_THRESHOLD = 0.9           # near_duplicate@1: token-set (Jaccard) similarity
EXTERNAL_VERIFIER_CONCURRENCY = 2        # headless setting external_verifier_concurrency overrides per run
EXTERNAL_VERIFIER_DEFAULT_TIMEOUT = 30
EXTERNAL_VERIFIER_MAX_TIMEOUT = 600
EXTERNAL_VERIFIER_MAX_STDOUT_BYTES = 65_536

# Brief 3B Step 1 (contract v2 source records, crawl/sources.py): AI crawlers
# whose explicit robots.txt groups are reported as an AI-training signal.
# Reported only -- the scraper's own requests don't use these names.
AI_TRAINING_USER_AGENTS = ("GPTBot", "ChatGPT-User", "OAI-SearchBot", "CCBot", "Google-Extended", "ClaudeBot",
                           "anthropic-ai", "Claude-Web", "Applebot-Extended", "PerplexityBot", "Bytespider",
                           "meta-externalagent", "cohere-ai", "Amazonbot")

# Count terminally failed pages toward max_pages (crawl/frontier.py
# count_failed_toward_budget). Off by default = the original budget
# (done/skipped_extract/mapped only). On: genuine fetch/extraction failures
# also consume the budget -- robots.txt disallows never do -- so a run whose
# pages keep failing stops at its budget instead of claiming page after page
# (ROADMAP.md #54: 247 pages / 741 fetches against max_pages=20).
MAX_PAGES_COUNTS_FAILED = os.getenv("MAX_PAGES_COUNTS_FAILED", "false").strip().lower() == "true"

# LLM circuit breaker (crawl/circuit_breaker.py; Brief 1 3c item C). Off by
# default: LLM_BREAKER_THRESHOLD unset/0 = no breaker, original behaviour.
# Set to N to open the breaker after N consecutive rate-limited LLM calls:
# extract workers then WAIT at its gate (a deliberate, documented exception
# to "rate limits release the row" -- see CLAUDE.md) instead of releasing
# page after page into an exhausted quota (the 741-429 run, ROADMAP #54).
# The wait is the provider's own retry hint when it sends one, else
# LLM_BREAKER_BASE_COOLDOWN_SECONDS doubling per trip up to
# LLM_BREAKER_MAX_COOLDOWN_SECONDS; then one probe call decides. After
# LLM_BREAKER_MAX_TRIPS trips, or at once on a daily-quota signal (a quota
# hint naming a per-day quota, or an UNCAPPED provider retry hint of at
# least LLM_BREAKER_STOP_IF_RETRY_AFTER_SECONDS), the run stops cleanly,
# leaving its rows resumable.
LLM_BREAKER_THRESHOLD = int(os.getenv("LLM_BREAKER_THRESHOLD", "0") or 0)
LLM_BREAKER_BASE_COOLDOWN_SECONDS = 60.0
LLM_BREAKER_MAX_COOLDOWN_SECONDS = 900.0
LLM_BREAKER_MAX_TRIPS = 3
LLM_BREAKER_STOP_IF_RETRY_AFTER_SECONDS = 3600.0

# ChatOpenAI's max_retries: the openai SDK retries 429/5xx itself, invisibly,
# before our code sees an error (honouring Retry-After up to 60s). 2 is the
# SDK/LangChain default, so the default here changes nothing. With the
# breaker on it is forced to 0 (effective_llm_sdk_max_retries) so every 429
# reaches the breaker -- otherwise one observed 429 can mean three.
LLM_SDK_MAX_RETRIES = int(os.getenv("LLM_SDK_MAX_RETRIES", "2"))


def effective_llm_sdk_max_retries() -> int:
    return 0 if LLM_BREAKER_THRESHOLD > 0 else LLM_SDK_MAX_RETRIES

# Network timeouts for the two real HTTP call sites behind asyncio.to_thread
# in llm_factory.py (LESSONS_LEARNED.md #56, ROADMAP.md #40) -- neither had
# any timeout at all before this: LocalOllamaEmbeddings' requests.post()
# calls had no timeout= argument, and ChatOpenAI ended up with
# Timeout(timeout=None) on its real underlying httpx.Client (confirmed by
# inspecting the live object, not the constructor signature -- see #56).
# A hung connection at either site would otherwise wedge an
# asyncio.to_thread slot forever. Values set well above real observed
# maxima, not guessed low, so a genuinely slow-but-working call is never
# mistaken for a dead one:
# - LLM_EXTRACT_TIMEOUT_SECONDS: real extraction calls observed up to
#   153.5s in a real run (#56); 600s matches the openai SDK's own default
#   (the one langchain_openai's wrapper was silently dropping).
# - OLLAMA_EMBED_TIMEOUT_SECONDS: local calls, observed 2-6s; 60s is
#   generous headroom, not tight, since a local call taking anywhere near
#   that long already indicates something is wrong with the local Ollama
#   server, not just "slow."
LLM_EXTRACT_TIMEOUT_SECONDS = 600
OLLAMA_EMBED_TIMEOUT_SECONDS = 60
# Jev's own docs: 70-500ms end-to-end observed, "40x-200x faster" than a
# frontier LLM call. 30s is generous headroom (like OLLAMA_EMBED_TIMEOUT_
# SECONDS above, not tight) given that profile -- not LLM_EXTRACT_TIMEOUT_
# SECONDS's 600s, which is sized for a genuinely slow chat completion, a
# different kind of call entirely. Not yet confirmed against a real call
# (no TYPESAFE_API_KEY in this environment) -- see SYSTEM_ONE_MODELS.md.
JEV_TIMEOUT_SECONDS = 30

# Section granularity for canonical records / export-time filenames
# (sectioning.py) -- how many leading URL path segments count as one
# "section". A deep site with depth=full-path would produce hundreds of
# near-empty per-section files at export time; 2 was picked as a
# reasonable default, not measured against a real deep site yet.
SECTION_DEPTH = 2

class LLMProvider(str, Enum):
    OPENAI = "openai"
    NVIDIA = "nvidia"
    OLLAMA = "ollama"
    OLLAMA_CLOUD = "ollama_cloud"
    GEMINI = "gemini"

# Providers with a working, wired-up embeddings path. OLLAMA_CLOUD is
# deliberately excluded: verified 2026-08-18 against docs.ollama.com that
# Ollama Cloud has no embedding models at all (the full cloud model catalog
# at ollama.com/search?c=cloud lists 16 chat/vision/tools models and zero
# embedding models; the embeddings docs page only documents localhost).
# See LESSONS_LEARNED.md #7. Do not route embeddings through OLLAMA_CLOUD --
# there is nothing at the other end to answer that request.
#
# GEMINI is ALSO deliberately excluded here despite actually having a real,
# working, free-tier embeddings endpoint (gemini-embedding-2, via the same
# OpenAI-compat /v1/embeddings path used for chat -- see
# SYSTEM_ONE_MODELS.md's Gemini section). get_llm() below hardcodes
# LocalOllamaEmbeddings for every provider regardless of this set, so
# nothing currently reads this set to make an embeddings-routing decision
# -- it's a documentation-only record of "could this provider serve
# embeddings," not live logic. Not wired up on purpose: switching the
# embeddings path is a separate decision from adding Gemini as a chat
# provider, not bundled into it here.
EMBEDDING_CAPABLE_PROVIDERS = {
    LLMProvider.OPENAI,
    LLMProvider.NVIDIA,
    LLMProvider.OLLAMA,
}

class LLMConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None

def get_llm_config(provider: LLMProvider) -> LLMConfig:
    if provider == LLMProvider.OPENAI:
        return LLMConfig(
            base_url=None,
            api_key=os.getenv("OPENAI_API_KEY")
        )
    elif provider == LLMProvider.NVIDIA:
        return LLMConfig(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=os.getenv("NVIDIA_API_KEY")
        )
    elif provider == LLMProvider.OLLAMA:
        return LLMConfig(
            base_url="http://localhost:11434/v1",
            api_key="ollama"
        )
    elif provider == LLMProvider.OLLAMA_CLOUD:
        # Chat/extraction only -- see EMBEDDING_CAPABLE_PROVIDERS above.
        #
        # base_url's correctness was originally unverified against Ollama's
        # own docs (docs.ollama.com's OpenAI-compatibility page only ever
        # documents `http://localhost:11434/v1` and never mentions
        # ollama.com; only the native `/api/chat` protocol was documented
        # for cloud). CONFIRMED WORKING (2026-08-18, step 4): a real
        # end-to-end call through ChatOpenAI against this exact base_url,
        # with model "deepseek-v4-flash", succeeded (see
        # LESSONS_LEARNED.md #10-11). If a future call gets 401, check the
        # env var loaded correctly before suspecting this endpoint again --
        # that's what actually happened here first.
        return LLMConfig(
            base_url="https://ollama.com/v1",
            api_key=os.getenv("OLLAMA_API_KEY")
        )
    elif provider == LLMProvider.GEMINI:
        # OpenAI-compat endpoint confirmed against ai.google.dev/gemini-api/
        # docs/openai (2026-09-22) -- see SYSTEM_ONE_MODELS.md. Chat only
        # here; embeddings deliberately not wired despite Gemini having a
        # working one (see EMBEDDING_CAPABLE_PROVIDERS above).
        return LLMConfig(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=os.getenv("GEMINI_API_KEY")
        )
    raise ValueError(f"Unsupported provider: {provider}")

class OutputPreferences(BaseModel):
    unified_jsonl: bool = False
    split_jsonl: bool = False
    build_rag: bool = False

# ---------------------------------------------------------------------------
# System One decision models (Jev/Laya) -- switchable alternative to the
# traditional LLM-call-based decision at every judgment point in the
# pipeline (branch selection, relevance gate, semantic dedup, sample
# quality scoring). See SYSTEM_ONE_MODELS.md for what each provider
# actually is; this is config only -- the decision interface itself and
# its wiring into call sites is separate follow-on work (not built by this
# change). Traditional stays the default and must keep working exactly as
# today regardless of anything below.
class SystemOneProvider(str, Enum):
    JEV = "jev"
    LAYA = "laya"

# false (default): every decision point uses the current (traditional)
# method. true: every decision point uses SYSTEM_ONE_PROVIDER instead.
# This is a single global switch, not per-decision-point, matching the
# instruction that traditional-vs-System-One is one setting applied
# everywhere, not a per-call-site knob.
USE_SYSTEM_ONE = os.getenv("USE_SYSTEM_ONE", "false").strip().lower() == "true"

# Only meaningful (and only validated) when USE_SYSTEM_ONE is true.
SYSTEM_ONE_PROVIDER = os.getenv("SYSTEM_ONE_PROVIDER")

# Jev, Laya, and the traditional method each produce a differently-shaped
# score distribution -- confirmed empirically, not assumed: dedup's single
# shared 0.4 threshold (originally tuned for string-similarity ratios) was
# wrong for all three methods when actually measured (see LESSONS_LEARNED.md's
# Step 5/6 entries). Reusing one number across methods is a category error,
# not a simplification, so thresholds are keyed by (decision_point, method)
# here instead of one constant per decision point. "select"/"reject" apply
# to a 3-way noul decision (branch selection); a decision point with a
# single accept/drop cutoff (dedup, sample quality) only defines "select",
# used as its sole cutoff. The relevance gate's threshold is normally a
# per-run, user-chosen value (main.py's extract_threshold/follow_threshold
# prompts) rather than a fixed config default -- its entries here are the
# suggested default shown for each method, not an enforced value; main.py's
# prompt reads calibrate_threshold_default() to pick which one to show.
#
# Current values are either inherited from before per-method thresholds
# existed (branch_selection: still the original reasoned-not-measured
# 0.7/0.3, shared by all methods because Step 5's real comparison found
# them roughly sound for Jev specifically, not yet for laya) or explicitly
# uncalibrated placeholders (dedup, sample_quality: no method-specific
# number has been measured yet) -- calibrate_threshold_default() and
# DECISION_THRESHOLDS exist so a real calibration (like Step 6's relevance-
# gate measurement) has somewhere real to put its answer, not to claim
# these particular numbers are already trustworthy.
DECISION_THRESHOLDS: dict[tuple[str, str], dict[str, float]] = {
    ("branch_selection", "traditional"): {"select": 0.7, "reject": 0.3},
    ("branch_selection", "jev"): {"select": 0.7, "reject": 0.3},
    ("branch_selection", "laya"): {"select": 0.7, "reject": 0.3},
    ("relevance_gate", "traditional"): {"select": 0.50},  # the measured Phase 1/2 default
    ("relevance_gate", "jev"): {"select": 0.50},  # placeholder until calibrated -- see LESSONS_LEARNED.md
    ("relevance_gate", "laya"): {"select": 0.50},  # placeholder, not yet tested at all
    ("semantic_dedup", "traditional"): {"select": 0.4},
    ("semantic_dedup", "jev"): {"select": 0.4},
    ("semantic_dedup", "laya"): {"select": 0.4},
    ("sample_quality", "traditional"): {"select": 0.0},  # 0.0 = off/log-only, matches the gate's own convention
    ("sample_quality", "jev"): {"select": 0.0},
    ("sample_quality", "laya"): {"select": 0.0},
}


def get_threshold(decision_point: str, method: str, key: str = "select") -> float:
    return DECISION_THRESHOLDS[(decision_point, method)][key]


def relevance_gate_threshold_default(method: str) -> float:
    """What main.py's extract_threshold/follow_threshold prompts should
    show as the default for the currently-active method -- the same
    number for every gate call in a run (one method is active at a time),
    never silently reused across methods."""
    return get_threshold("relevance_gate", method)


class SystemOneConfigError(Exception):
    pass


def validate_system_one_config() -> None:
    """Fails loudly at startup -- not on the first decision an hour into a
    crawl, which is what happens if this is left to whatever call site
    happens to hit a misconfigured decision first. Pure/offline: checks
    the SHAPE of what's configured (provider name valid, required key
    present, required package importable) -- never makes a network call,
    so it can't itself be slow or flaky. Call this from any entry point
    that actually makes decisions (main(), headless_crawl(),
    suggest_selection_command() today) before doing real work; read-only
    commands (--graph, --live-status, --cancel, etc.) never call it, since
    they never reach a decision point regardless of this config.
    """
    if not USE_SYSTEM_ONE:
        return
    valid_providers = {p.value for p in SystemOneProvider}
    if SYSTEM_ONE_PROVIDER not in valid_providers:
        raise SystemOneConfigError(
            f"USE_SYSTEM_ONE=true requires SYSTEM_ONE_PROVIDER to be one of "
            f"{sorted(valid_providers)}, got {SYSTEM_ONE_PROVIDER!r}."
        )
    if SYSTEM_ONE_PROVIDER == SystemOneProvider.JEV.value and not os.getenv("TYPESAFE_API_KEY"):
        raise SystemOneConfigError(
            "SYSTEM_ONE_PROVIDER=jev requires TYPESAFE_API_KEY to be set (see .env.example)."
        )
    if SYSTEM_ONE_PROVIDER == SystemOneProvider.LAYA.value and importlib.util.find_spec("laya") is None:
        raise SystemOneConfigError(
            "SYSTEM_ONE_PROVIDER=laya requires the 'laya' package, which is not "
            "installed. See SYSTEM_ONE_MODELS.md before installing it -- torch/"
            "transformers may conflict with this project's numpy<2 pin."
        )
