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

# Variable-count, anti-reword, table-coverage prompt. History:
# - Fixed "3 to 5 pairs" (pre-Phase-3) produced near-identical paraphrase
#   padding on 98.9% of real chunks regardless of content richness
#   (LESSONS_LEARNED.md #33's 2B measurement).
# - Phase 3 Step 3's variable-count/anti-reword version fixed that (a
#   real 12-chunk A/B test cut pair count 56 -> 43), but a later
#   diagnosis (LESSONS_LEARNED.md #43/#45, ROADMAP #32) found it
#   under-covers table/list rows relative to prose: confirmed on two
#   unrelated generators (manim/Sphinx, FastAPI/mkdocstrings), 894 real
#   table rows measured, only 81.9% mentioned in any pair, and the
#   misses weren't just thin rows -- 18.1% of *richly-described* rows
#   (75 distinct names) got zero coverage.
# - Rule 1 below adds a coverage rule scoped specifically to tables/
#   lists of named items, alongside the unchanged anti-padding rule for
#   everything else -- tested against 3 alternatives offline
#   (LESSONS_LEARNED.md #46) and verified by manually reading every
#   output, not just an automated redundancy score (which produced a
#   false positive on this exact kind of output -- see #40/#46): this
#   phrasing closed the coverage gap to 100% rich-row coverage on both
#   generators with zero bare-row padding and no fragmentation
#   regression, where a two-step "enumerate then one-pair-each"
#   alternative matched the coverage number but genuinely fragmented
#   prose (FastAPI tutorial pairs 5 -> 12 on one chunk) and re-split a
#   multi-aspect method that this version and the prior baseline both
#   correctly kept as one pair.
QA_EXTRACTION_SYSTEM_PROMPT = """You are an expert training data generator. Your task is to read the provided text and generate diverse, high-quality instruction-response pairs for fine-tuning a Large Language Model.

CRITICAL RULES:
1. If the text contains a table, list, or enumeration of distinct named items (parameters, attributes, methods, classes, etc.), generate one pair per item that has real descriptive content -- do not skip an item just because its description is short, as long as it says something real about that item.
2. For all other content, generate one pair per genuinely distinct fact or concept -- not a fixed count. Thin content that only supports one or two real questions should produce only one or two pairs; do not pad with restatements to reach a target number.
3. Never generate two pairs that ask about the same underlying fact from a different angle. Before adding a pair, check it against the ones you've already written -- if it's really the same question reworded, skip it instead.
4. The 'instruction' must be a specific question or command that a user would realistically ask.
5. The 'instruction' MUST be entirely self-contained. Never use pronouns like "he", "it", or "this company".
6. The 'response' must be accurate, detailed, and derived ONLY from the provided text.
7. If the text is just a navigation menu or footer with no real content, return an empty list.

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
