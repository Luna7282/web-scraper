"""Sample quality scoring: is this extracted Q&A pair well-formed,
accurate to its source, and useful as a training example? No prior
method exists for this decision (unlike the other three) -- per step 3's
instruction, "where a decision point has no traditional method today,
the traditional path is an LLM call", so traditional here is
LLMTraditionalBackend, not a bespoke implementation.

Opt-in (crawl/pipeline.py::extract_worker's quality_backend param,
config.SAMPLE_QUALITY_THRESHOLD) -- unlike the relevance gate/branch
selection, which already ran before this interface existed, adding a
mandatory extra LLM call to every existing crawl by default would itself
be a behavior/cost change this project's "traditional stays the default"
rule exists to prevent.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from decisions import DecisionBackend, LLMTraditionalBackend, get_backend

ChatFn = Callable[[str, str], Awaitable[str]]

SAMPLE_QUALITY_SYSTEM_PROMPT = """You are evaluating whether an extracted instruction-response training pair is good enough to keep.

A pair is good if all of the following hold:
- The response is accurate to, and directly supported by, its stated source text -- no invented facts.
- The instruction is specific and self-contained -- no dangling pronouns like "it" or "this company" with nothing to refer to.
- The response actually answers the instruction, not a tangential or generic restatement of it.
- The pair is substantive, not boilerplate (navigation text, a bare "see above", an empty acknowledgment).

Answer false only when the pair genuinely fails one of these -- a merely terse but accurate answer is still good; don't reject out of excessive caution."""


def make_quality_backend(chat_fn: ChatFn) -> DecisionBackend:
    traditional = LLMTraditionalBackend(chat_fn, system_prompt=SAMPLE_QUALITY_SYSTEM_PROMPT)
    return get_backend(traditional)
