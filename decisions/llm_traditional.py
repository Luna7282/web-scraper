"""LLMTraditionalBackend: "the traditional path is an LLM call" for a
decision point that has no existing bespoke method today (Step 3's
instruction). This is generic on purpose -- but NOT used by branch
selection or the relevance gate, both of which already have a working,
tuned traditional method (crawl/branch_select.py's own prompt/parsing,
content/relevance.py's embedding similarity) that has to keep working
exactly as today. Reusing this generic prompt for either would mean a
*different* prompt answering the same decision -- exactly the behavior
change "traditional stays the default" rules out, even if the new prompt
were just as good. This class exists for decision points with no prior
method at all -- today, that's sample quality scoring.
"""
from __future__ import annotations

import json
import re
from typing import Awaitable, Callable

from decisions.backend import DecisionBackend, DecisionError
from decisions.types import NoulAnswer

# (system_prompt, user_prompt) -> raw response text. Same shape as
# main.py's existing extract_fn/classify_fn closures around ChatOpenAI.
ChatFn = Callable[[str, str], Awaitable[str]]

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _salvage_json_array(raw_text: str) -> list:
    """Same salvage order as content/extraction.py::parse_qa_json and
    crawl/branch_select.py::parse_branch_classification: raw, then
    fence-stripped, then the substring between the first '[' and last ']'
    -- prose-wrapped JSON is the common real-provider shape, not rare."""
    candidates = [raw_text.strip()]
    fenced = _FENCE_RE.sub("", raw_text).strip()
    if fenced != candidates[0]:
        candidates.append(fenced)
    start, end = raw_text.find("["), raw_text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw_text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed
    raise DecisionError(f"could not parse a JSON array from response: {raw_text[:200]!r}")


class LLMTraditionalBackend(DecisionBackend):
    def __init__(self, chat_fn: ChatFn, system_prompt: str):
        self._chat_fn = chat_fn
        self._system_prompt = system_prompt

    async def noul(
        self, state: str, instructions: str, criteria: dict[str, str] | None = None,
    ) -> NoulAnswer:
        return (await self.noul_many({"_": (state, instructions, criteria)}))["_"]

    async def noul_many(
        self, items: dict[str, tuple[str, str, dict[str, str] | None]],
    ) -> dict[str, NoulAnswer]:
        """One chat completion for every item -- the traditional path's
        actual cost-efficiency property (unlike Jev/Laya, a chat
        completion is neither fast nor free, so batching genuinely
        matters here, not just as an optimization)."""
        prompt = self._build_prompt(items)
        raw = await self._chat_fn(self._system_prompt, prompt)
        parsed = _salvage_json_array(raw)
        by_key = {str(item.get("key")): item for item in parsed if isinstance(item, dict)}

        results: dict[str, NoulAnswer] = {}
        for key in items:
            item = by_key.get(key)
            if item is None or "answer" not in item:
                results[key] = NoulAnswer(
                    value=0.5, method="traditional",
                    reason="model did not return an answer for this item", raw={},
                )
                continue
            answer = item["answer"]
            value = 1.0 if answer is True else 0.0 if answer is False else 0.5
            results[key] = NoulAnswer(
                value=value, method="traditional", reason=item.get("reason"), raw=item,
            )
        return results

    def _build_prompt(
        self, items: dict[str, tuple[str, str, dict[str, str] | None]],
    ) -> str:
        lines = [f"Evaluate each of the following {len(items)} item(s) independently.", ""]
        for key, (state, instructions, criteria) in items.items():
            lines.append(f"- key: {key}")
            lines.append(f"  state: {state}")
            lines.append(f"  question: {instructions}")
            if criteria:
                lines.append(f"  criteria: {criteria}")
            lines.append("")
        lines.append(
            "Respond with a JSON array, one object per item, each with exactly "
            'these keys: "key" (copied exactly), "answer" (true, false, or null '
            'if you genuinely cannot tell), and "reason" (one sentence).'
        )
        return "\n".join(lines)
