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
    
    # 1. Chat Model -> Routes to OLLAMA CLOUD
    llm = ChatOpenAI(
        model=model_name,
        api_key=config.api_key if config.api_key else "dummy",
        base_url=config.base_url,
    )
    
    # 2. Embedding Model -> Native Local HTTP (Zero proxy bugs)
    embeddings = LocalOllamaEmbeddings(model_name=embedding_model_name)
    
    return llm, embeddings