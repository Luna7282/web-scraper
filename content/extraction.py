"""Parsing an LLM's raw text response into Q&A pairs, with an explicit
salvage-vs-fail decision -- prose wrapped around a JSON block is the common
case for a real provider response, not an edge case, so it gets tried
before giving up.

Deliberately separate from output_manager.py's _generate_qa (which does its
own simpler fence-stripping and silently swallows all parse failures as an
empty list): that conflates "the LLM legitimately found nothing
extractable" with "the response was malformed and extraction genuinely
failed," which need different handling upstream (the former is a normal
outcome; the latter should be retried, not silently treated as a clean
zero-pairs result). MalformedExtractionError makes that distinction
explicit for callers.
"""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Variable-count, anti-reword prompt -- replaces a fixed "3 to 5 pairs"
# instruction that produced near-identical paraphrase padding on 98.9% of
# real chunks regardless of content richness (LESSONS_LEARNED.md #33's
# 2B measurement). A real 12-chunk A/B test against the fixed-count
# version (reusing exact stored source_chunk text, zero new fetches) cut
# pair count 56 -> 43 with 6 of 7 reductions being pure restatement
# removal or improved coverage at the same count; one chunk showed a
# small real loss, recorded below rather than left to be rediscovered.
QA_EXTRACTION_SYSTEM_PROMPT = """You are an expert training data generator. Your task is to read the provided text and generate up to 5 diverse, high-quality instruction-response pairs for fine-tuning a Large Language Model.

CRITICAL RULES:
1. Generate one pair per genuinely distinct fact, concept, or capability described in the text -- not a fixed count. Thin content that only supports one or two real questions should produce only one or two pairs; do not pad with restatements to reach a target number.
2. Never generate two pairs that ask about the same underlying fact from a different angle. Before adding a pair, check it against the ones you've already written -- if it's really the same question reworded, skip it instead.
3. The 'instruction' must be a specific question or command that a user would realistically ask.
4. The 'instruction' MUST be entirely self-contained. Never use pronouns like "he", "it", or "this company".
5. The 'response' must be accurate, detailed, and derived ONLY from the provided text.
6. If the text is just a navigation menu or footer with no real content, return an empty list.

Format your output EXACTLY as a JSON array of objects, where each object has an "instruction" key and a "response" key."""


class MalformedExtractionError(Exception):
    pass


def parse_qa_json(text: str) -> list[dict]:
    """Returns a list of {"instruction":..., "response":...} dicts --
    possibly empty, which is a valid outcome (the LLM parsed cleanly and
    said there was nothing extractable). Raises MalformedExtractionError
    only when no candidate parse produces valid JSON at all.

    Tries, in order: (1) the raw text as-is, (2) stripped of a markdown
    code fence, (3) the substring between the first '[' and last ']' in
    the text -- this is what salvages "Here's the extracted content:
    [...]  Let me know if you need anything else!"-style prose wrapping,
    which is the common shape for a real provider response, not a rare one.
    """
    candidates = [text.strip()]

    fenced = _FENCE_RE.sub("", text).strip()
    if fenced != candidates[0]:
        candidates.append(fenced)

    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            continue
        return [
            p for p in parsed
            if isinstance(p, dict) and "instruction" in p and "response" in p
        ]

    raise MalformedExtractionError(
        f"could not parse a JSON array from response: {text[:200]!r}"
    )
