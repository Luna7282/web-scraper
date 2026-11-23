"""Per-call provenance published by an LLM call (Brief 3B Step 1).

call_served_model: the model the provider's response said it ran (None if
absent), set by main.py's extract_fn for each call and read back by
extract_worker in the same task, right after it awaits the call -- the same
ContextVar pattern as crawl.timing.call_usage, so extract_fn's
`content -> str` contract is unchanged.
"""

from __future__ import annotations

from contextvars import ContextVar

call_served_model: ContextVar[str | None] = ContextVar("call_served_model", default=None)


def served_model_from_message(message) -> str | None:
    meta = getattr(message, "response_metadata", None) or {}
    value = meta.get("served_model")
    return value if isinstance(value, str) and value else None
