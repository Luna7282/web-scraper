import os
from enum import Enum
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# Frontier / crawl-pipeline knobs (frontier.py). Chunk size/overlap and
# relevance thresholds are step 6/7's job, not added speculatively here.
MAX_RETRIES = 3
FOLLOW_GATE_EXEMPT_DEPTH = 0  # seed's own children always promote regardless of score

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
