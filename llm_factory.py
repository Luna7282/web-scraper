from crawl4ai import cache_context
from langchain_openai import ChatOpenAI
from config import LLMProvider, get_llm_config, LLM_EXTRACT_TIMEOUT_SECONDS, OLLAMA_EMBED_TIMEOUT_SECONDS
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

def get_llm(
    provider_name: str,
    model_name: str = "deepseek-v4-flash",
    embedding_model_name: str = "nomic-embed-text"
):
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
    llm = ChatOpenAI(
        model=model_name,
        api_key=config.api_key if config.api_key else "dummy",
        base_url=config.base_url,
        timeout=LLM_EXTRACT_TIMEOUT_SECONDS,
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