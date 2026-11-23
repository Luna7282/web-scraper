from crawl4ai import cache_context
from langchain_openai import ChatOpenAI
from config import (
    LLMProvider, get_llm_config, LLM_EXTRACT_TIMEOUT_SECONDS, OLLAMA_EMBED_TIMEOUT_SECONDS,
    effective_llm_sdk_max_retries,
)
import requests

# Custom native embedding class that bypasses OpenAI SDK transport bugs entirely
class LocalOllamaEmbeddings:
    def __init__(self, model_name: str = "nomic-embed-text", timeout_seconds: float = OLLAMA_EMBED_TIMEOUT_SECONDS):
        self.model_name = model_name
        self.url = "http://localhost:11434/api/embeddings"
        self.timeout_seconds = timeout_seconds

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        embeddings = []
        for text in texts:
            response = requests.post(
                self.url,
                json={"model": self.model_name, "prompt": text},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            embeddings.append(response.json()["embedding"])
        return embeddings

    def embed_query(self, text: str) -> list[float]:
        response = requests.post(
            self.url,
            json={"model": self.model_name, "prompt": text},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()["embedding"]

class ServedModelChatOpenAI(ChatOpenAI):
    """ChatOpenAI that also records the model the provider SAID it served.

    langchain_openai fills response_metadata["model_name"] with
    `response.get("model", self.model_name)` -- the model we ASKED for when
    the provider omits the field -- so "absent" can't be told apart from
    "same as requested". This puts the raw response `model` (None if absent
    or empty) in response_metadata["served_model"] (Brief 3B Step 1,
    provenance). Nothing else changes: same requests, same text."""

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        raw = response.get("model") if isinstance(response, dict) else getattr(response, "model", None)
        served = raw if isinstance(raw, str) and raw.strip() else None
        for generation in result.generations:
            generation.message.response_metadata["served_model"] = served
        return result


def get_llm(
    provider_name: str,
    model_name: str = "deepseek-v4-flash",
    embedding_model_name: str = "nomic-embed-text",
    max_retries: int | None = None,
    api_key: str | None = None,
):
    """max_retries: the openai SDK's own silent retries. None = config's
    effective value (LLM_SDK_MAX_RETRIES, default 2 -- the SDK default, so
    unchanged; 0 when the LLM circuit breaker is on).

    api_key: an explicit key for this run (headless contract v1's
    `api_key_env`), used instead of the provider's usual environment variable.
    None = today's behaviour. Never logged."""
    provider = LLMProvider(provider_name.lower())
    config = get_llm_config(provider)

    # 1. Chat model -> OpenAI-compatible endpoint for whichever provider was
    #    selected (OpenAI, NVIDIA NIM, Ollama local, or Ollama cloud).
    #    timeout= is load-bearing, not decorative -- without it,
    #    langchain_openai's httpx wrapper ends up with Timeout(timeout=None)
    #    (confirmed by inspecting the live client object, not assumed --
    #    see LESSONS_LEARNED.md #56), meaning a hung connection blocks its
    #    asyncio.to_thread slot forever instead of eventually raising into
    #    the existing retry path.
    llm = ServedModelChatOpenAI(
        model=model_name,
        api_key=api_key or (config.api_key if config.api_key else "dummy"),
        base_url=config.base_url,
        timeout=LLM_EXTRACT_TIMEOUT_SECONDS,
        max_retries=effective_llm_sdk_max_retries() if max_retries is None else max_retries,
    )

    # 2. Embedding model -> always native local Ollama HTTP, regardless of
    #    which provider was picked for chat above. This is deliberate, not
    #    an oversight: Ollama Cloud has no embedding models to route to (see
    #    EMBEDDING_CAPABLE_PROVIDERS in config.py and LESSONS_LEARNED.md #7),
    #    and local Ollama embeddings must never go through an OpenAI-compat
    #    wrapper regardless (dynamic-port bug, LESSONS_LEARNED.md #3) -- so
    #    LocalOllamaEmbeddings is the only embeddings path until a real
    #    NVIDIA/OpenAI embeddings integration is wired up (tracked in
    #    ROADMAP.md, not done here).
    embeddings = LocalOllamaEmbeddings(model_name=embedding_model_name)

    return llm, embeddings