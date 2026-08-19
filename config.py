import os
from enum import Enum
from pydantic import BaseModel
from dotenv import load_dotenv

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

# Providers with a working, wired-up embeddings path. OLLAMA_CLOUD is
# deliberately excluded: verified 2026-08-18 against docs.ollama.com that
# Ollama Cloud has no embedding models at all (the full cloud model catalog
# at ollama.com/search?c=cloud lists 16 chat/vision/tools models and zero
# embedding models; the embeddings docs page only documents localhost).
# See LESSONS_LEARNED.md #7. Do not route embeddings through OLLAMA_CLOUD --
# there is nothing at the other end to answer that request.
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
    raise ValueError(f"Unsupported provider: {provider}")

class OutputPreferences(BaseModel):
    unified_jsonl: bool = False
    split_jsonl: bool = False
    build_rag: bool = False
