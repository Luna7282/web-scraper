"""Contract v2 preflights (Brief 3B Step 2, items 19 and 21): checks that run
before any page work, so a run fails in seconds with a clear message instead
of partway in.

- provider_preflight: ONE tiny call (config.PREFLIGHT_MAX_TOKENS), only when
  the run would make LLM calls anyway (never in a dry pass). A retired or
  unknown model (HTTP 410, or 404 naming the model) and a rejected key (401 /
  403) raise PreflightFailed -> config_error, exit 2. Anything else (429,
  5xx, a timeout) is a recorded warning: the breaker and the 5xx release
  path handle those during the run.
- embeddings_preflight: one embedding, only when the run needs embeddings
  (an intent, RAG, or top_k_chunks_by_relevance). Any failure raises
  PreflightFailed with what to do about it.

Errors are classified by status code and body text, never exception class
alone, and only a redacted, capped detail is ever kept.
"""

from __future__ import annotations

import asyncio
import time

import config
from crawl.pipeline import _redact_and_cap
from crawl.provenance import served_model_from_message
from crawl.timing import usage_from_message

EMBEDDING_REMEDIES = ("Options: start Ollama and run `ollama pull {model}`; or run without embeddings "
                      "(a blank intent, build_rag false, and an extraction strategy other than "
                      "top_k_chunks_by_relevance). A hosted embeddings backend isn't available yet (ROADMAP #62).")


class PreflightFailed(Exception):
    """The run can't start. The message says why and what to do; no secrets."""


def provider_status(e: BaseException):
    return getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)


def classify_provider_error(e: BaseException) -> str:
    """'retired' (410, or 404 naming a model), 'auth' (401/403), else 'other'."""
    status, body = provider_status(e), str(e).lower()
    if status == 410:
        return "retired"
    if status == 404 and "model" in body and any(s in body for s in ("not found", "does not exist", "not_found",
                                                                     "unknown model", "no such model")):
        return "retired"
    if status in (401, 403):
        return "auth"
    return "other"


def needs_embeddings(cfg: dict) -> bool:
    return bool(cfg.get("intent")) or bool(cfg.get("build_rag")) or (
        config.EXTRACTION_STRATEGY == config.ExtractionStrategy.TOP_K_CHUNKS_BY_RELEVANCE)


async def provider_preflight(llm, provider: str, model: str, cost=None) -> dict:
    from langchain_core.messages import HumanMessage

    started = time.perf_counter()
    try:
        response = await asyncio.to_thread(llm.invoke, [HumanMessage(content="Reply with the single word OK.")],
                                           max_tokens=config.PREFLIGHT_MAX_TOKENS)
    except Exception as e:  # noqa: BLE001 -- every failure is classified below
        kind, status = classify_provider_error(e), provider_status(e)
        if cost is not None:
            cost.record("preflight", "error")
        detail = _redact_and_cap(str(e))
        if kind == "retired":
            raise PreflightFailed(f"provider preflight: {provider} says model {model!r} is retired or unknown "
                                  f"(HTTP {status}); repick the model. Provider said: {detail}") from None
        if kind == "auth":
            raise PreflightFailed(f"provider preflight: {provider} rejected the API key (HTTP {status}); check "
                                  f"the key named by api_key_env") from None
        return {"status": "warning", "http_status": status, "error": type(e).__name__, "detail": detail,
                "seconds": round(time.perf_counter() - started, 3)}
    usage = usage_from_message(response)
    if cost is not None:
        cost.record("preflight", "ok", usage)
    return {"status": "ok", "served_model": served_model_from_message(response), "usage": usage,
            "seconds": round(time.perf_counter() - started, 3)}


async def embeddings_preflight(embeddings, model_name: str) -> dict:
    import requests

    started = time.perf_counter()
    remedies = EMBEDDING_REMEDIES.format(model=model_name)
    try:
        vector = await asyncio.to_thread(embeddings.embed_query, "preflight")
    except requests.exceptions.ConnectionError:
        raise PreflightFailed(f"embeddings preflight: local Ollama is not reachable at localhost:11434. "
                              f"{remedies}") from None
    except Exception as e:  # noqa: BLE001 -- every failure fails the preflight
        status, body = provider_status(e), str(e).lower()
        if status == 404 or "not found" in body:
            raise PreflightFailed(f"embeddings preflight: embedding model {model_name!r} is not available "
                                  f"in local Ollama. {remedies}") from None
        raise PreflightFailed(f"embeddings preflight failed ({type(e).__name__}: {_redact_and_cap(str(e))}). "
                              f"{remedies}") from None
    if not vector:
        raise PreflightFailed(f"embeddings preflight: {model_name!r} returned an empty vector. {remedies}")
    return {"status": "ok", "model": model_name, "dim": len(vector),
            "seconds": round(time.perf_counter() - started, 3)}
