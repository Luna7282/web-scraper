"""JevBackend: TypeSafe AI's hosted System One model. Plain `requests`,
same pattern as llm_factory.py's LocalOllamaEmbeddings -- the API surface
is small and well-typed enough that a new SDK dependency isn't needed
(see SYSTEM_ONE_MODELS.md's "open decision" note, resolved here in favor
of no new dependency). Exact request/response schema confirmed against
docs.typesafe.ai/api.md + quickstart + primitives, not memory.
"""
from __future__ import annotations

import asyncio

import requests

import config
from crawl.pipeline import RateLimitError
from decisions.backend import DecisionBackend, DecisionError
from decisions.types import NoulAnswer

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class JevBackend(DecisionBackend):
    def __init__(self, api_key: str, model: str = "jev-latest", timeout: float = config.JEV_TIMEOUT_SECONDS):
        if not api_key:
            raise DecisionError("JevBackend requires an api_key (TYPESAFE_API_KEY)")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def noul(
        self, state: str, instructions: str, criteria: dict[str, str] | None = None,
    ) -> NoulAnswer:
        question = {"type": "noul", "instructions": instructions}
        if criteria:
            question["criteria"] = criteria
        body = {"state": state, "model": self._model, "questions": {"_": question}}
        response = await asyncio.to_thread(self._post, body)
        answer = response["answers"]["_"]
        return NoulAnswer(value=answer["noul"], method="jev", raw=answer)

    def _post(self, body: dict) -> dict:
        try:
            response = requests.post(
                _ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            raise DecisionError(f"Jev request failed: {type(e).__name__}: {e}") from e

        if response.status_code in (429, 529):
            # Docs: retry with exponential backoff, not immediately -- same
            # rate-limit-releases-the-row contract every other LLM call in
            # this codebase already follows (crawl/pipeline.py's
            # RateLimitError -> mark_extract_outcome(..., backoff_seconds=)).
            raise RateLimitError(retry_after=None)
        if response.status_code == 401:
            raise DecisionError("Jev: missing or invalid TYPESAFE_API_KEY (401)")
        if response.status_code == 422:
            raise DecisionError(f"Jev: request validation failed (422): {response.text[:500]}")
        if response.status_code != 200:
            raise DecisionError(f"Jev: unexpected status {response.status_code}: {response.text[:500]}")
        return response.json()
