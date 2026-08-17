import os
from enum import Enum
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

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
        # base_url is UNVERIFIED against Ollama's own documentation. Confirmed
        # from docs.ollama.com (2026-08-18): cloud models are documented as
        # reachable via the native API (`https://ollama.com/api/chat`, via
        # Ollama's own Python/JS client or raw curl) with
        # `Authorization: Bearer $OLLAMA_API_KEY`. docs.ollama.com's
        # OpenAI-compatibility page documents only `http://localhost:11434/v1`
        # and never mentions ollama.com. `https://ollama.com/v1` mirrors
        # local's OpenAI-compat path and is reported by secondary/aggregator
        # sources, but no primary Ollama doc confirms it responds. Untested
        # end-to-end (no OLLAMA_API_KEY available this session) -- see
        # LESSONS_LEARNED.md #7 before relying on this in a real run. If it
        # doesn't work, the fallback is the native `/api/chat` protocol via
        # Ollama's own client, not another guessed OpenAI-compat path.
        return LLMConfig(
            base_url="https://ollama.com/v1",
            api_key=os.getenv("OLLAMA_API_KEY")
        )
    raise ValueError(f"Unsupported provider: {provider}")

class OutputPreferences(BaseModel):
    unified_jsonl: bool = False
    split_jsonl: bool = False
    build_rag: bool = False
