from crawl4ai import cache_context
from langchain_openai import ChatOpenAI
from config import LLMProvider, get_llm_config
import requests

# Custom native embedding class that bypasses OpenAI SDK transport bugs entirely
class LocalOllamaEmbeddings:
    def __init__(self, model_name: str = "nomic-embed-text"):
        self.model_name = model_name
        self.url = "http://localhost:11434/api/embeddings"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        embeddings = []
        for text in texts:
            response = requests.post(
                self.url,
                json={"model": self.model_name, "prompt": text}
            )
            response.raise_for_status()
            embeddings.append(response.json()["embedding"])
        return embeddings

    def embed_query(self, text: str) -> list[float]:
        response = requests.post(
            self.url,
            json={"model": self.model_name, "prompt": text}
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
    llm = ChatOpenAI(
        model=model_name,
        api_key=config.api_key if config.api_key else "dummy",
        base_url=config.base_url,
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