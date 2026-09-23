"""Relevance scoring: how well a page matches the user's intent.

Pure math (cosine_similarity, extract_headings, chunk_text) is kept
separate from the embedding I/O (injected via EmbedFn) so the scoring
mechanics are unit-testable with a stub, independent of which strategy
(whole-page / headings / max-over-chunks) turns out to actually
discriminate -- that choice is decided by measurement (see
tests/measure_relevance_distribution.py), not assumed here.
"""
from __future__ import annotations

import math
import re
from typing import Awaitable, Callable

from langchain_text_splitters import RecursiveCharacterTextSplitter

from decisions import DecisionBackend, NoulAnswer, get_backend

EmbedFn = Callable[[str], Awaitable[list[float]]]

# Fenced code block, opening ``` (optional language tag) to a closing ```
# on its own line. Used to keep chunk_text() from ever cutting a code
# example in half -- measured on a real manim crawl (78 chunks, 38 with
# code, 6 with an odd/unbalanced fence count = split mid-block) before
# this was added.
_FENCE_BLOCK_RE = re.compile(r"^```[^\n]*\n.*?\n^```[ \t]*$", re.MULTILINE | re.DOTALL)

# nomic-embed-text's context_length is 2048 tokens; real markdown (heavy on
# links/punctuation) tokenizes far less efficiently than plain prose --
# 8000 chars measurably 500'd with "input length exceeds context length"
# against real fixture content. 4000 verified safe across the most
# link-dense fixtures tested; also matches output_manager.py's existing
# 4000-char truncation for the extraction LLM call, reusing an established
# working precedent rather than picking a new number. Not tied to a
# specific model's exact limit -- deliberately conservative.
MAX_EMBED_CHARS = 4000


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# Heading-level page furniture that leaks into extract_headings() because
# it's a real ATX heading (a genuine <h2>, not nav/header/footer/aside),
# so content/chrome_strip.py's tag/role-based exclusion can't catch it --
# confirmed against a real page's raw HTML: "Related tags" sits in
# <section class="post-extras">, not any excluded landmark tag (see
# LESSONS_LEARNED.md #75/#79). Found by measuring which heading lines
# repeat verbatim across many otherwise-unrelated real pages (125/313 of
# the archived Cloudflare corpus for these two) -- a length threshold was
# considered and rejected: manim's real single-class-name page titles are
# as short as 6 characters ("# Cube"), so any length cutoff generous
# enough to admit those would also admit these chrome lines, and any
# cutoff tight enough to exclude these would exclude real manim headings
# too. Same theme-specific-phrase, data-not-logic convention as
# chrome_strip.py's DEFAULT_TEXT_PATTERNS -- extend via the patterns
# param, don't hardcode a new phrase into the matching logic itself.
DEFAULT_HEADING_CHROME_PATTERNS = [
    "related tags",
    "subscribe to receive notifications of new posts",
]

_HEADING_PREFIX_RE = re.compile(r"^#+\s*")


def _strip_chrome_headings(headings: str, patterns=None) -> str:
    """Drops any heading line whose text (after the leading #s) exactly
    matches a known page-furniture phrase, case-insensitively -- same
    "whole line, not substring" matching discipline as
    chrome_strip.py::strip_text_patterns(), so a real heading that merely
    mentions one of these phrases in a longer title isn't dropped."""
    normalized = {p.strip().lower() for p in (patterns if patterns is not None else DEFAULT_HEADING_CHROME_PATTERNS)}
    lines = [
        line for line in headings.split("\n")
        if _HEADING_PREFIX_RE.sub("", line).strip().lower() not in normalized
    ]
    return "\n".join(lines)


def extract_headings(markdown: str) -> str:
    """Concatenated markdown ATX headings -- a candidate scoring unit much
    closer in length/specificity to a short intent string than a full
    page is, which is exactly the scale mismatch item 1 is worried about."""
    lines = [line.strip() for line in markdown.split("\n") if line.strip().startswith("#")]
    return "\n".join(lines)


def chunk_text(text: str, chunk_size: int = 2000, chunk_overlap: int = 0) -> list[str]:
    """Splits like RecursiveCharacterTextSplitter, except a fenced code
    block is never cut: it's chunked as one atomic piece, overflowing
    chunk_size rather than being split, while the surrounding prose is
    chunked normally around it. No overlap is added at a code block's
    edges -- duplicating text into or out of a fenced block would break
    the same guarantee this exists to provide."""
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = []
    pos = 0
    for m in _FENCE_BLOCK_RE.finditer(text):
        if m.start() > pos:
            chunks.extend(splitter.split_text(text[pos : m.start()]))
        chunks.append(m.group(0))
        pos = m.end()
    if pos < len(text):
        chunks.extend(splitter.split_text(text[pos:]))
    return chunks


async def score_whole_page(intent_embedding: list[float], content: str, embed_fn: EmbedFn) -> float:
    """Candidate strategy 1: embed the page (truncated) as one unit."""
    page_embedding = await embed_fn(content[:MAX_EMBED_CHARS])
    return cosine_similarity(intent_embedding, page_embedding)


async def score_headings(intent_embedding: list[float], content: str, embed_fn: EmbedFn) -> float:
    """Candidate strategy 2: embed just the headings -- shorter, closer in
    scale to the intent string. Falls back to whole-page if the content
    has no markdown headings at all."""
    headings = extract_headings(content)
    if not headings:
        return await score_whole_page(intent_embedding, content, embed_fn)
    heading_embedding = await embed_fn(headings[:MAX_EMBED_CHARS])
    return cosine_similarity(intent_embedding, heading_embedding)


async def score_max_chunk(
    intent_embedding: list[float], content: str, embed_fn: EmbedFn, chunk_size: int = 2000
) -> float:
    """Candidate strategy 3: split into chunks, embed each, take the max
    similarity -- a long page can be mostly irrelevant to an intent even
    when one section is exactly what the user wants; averaging or
    whole-page scoring would dilute that section's own strong match."""
    chunks = chunk_text(content, chunk_size=chunk_size)
    if not chunks:
        return 0.0
    scores = [
        cosine_similarity(intent_embedding, await embed_fn(chunk[:MAX_EMBED_CHARS]))
        for chunk in chunks
    ]
    return max(scores)


# score_headings picked from a real measurement (see LESSONS_LEARNED.md
# #17), not the default guess (max-over-chunks) -- it was the only one of
# the three candidates that ranked a real API reference page above a
# marketing page for an intent specifically about that API, at 1 embed
# call/page instead of max_chunk's 5-13.
GateScoreFn = Callable[[str, str], Awaitable[float]]

# Returned when there's no intent to score against -- always above any
# reasonable threshold, so the gate is structurally a no-op rather than
# extract_worker needing a special case for "no intent given".
_NO_GATE_SCORE = 1.0


async def _no_gate_score_fn(url: str, content: str) -> float:
    return _NO_GATE_SCORE


def gate_state(content: str) -> str:
    """The unit the relevance gate actually judges -- headings if the page
    has any *real* ones, else the truncated whole page. Shared by every
    gate backend (traditional and System One both judge the SAME unit) --
    important for Jev/Laya specifically, not just Laya's small context
    budget: Jev's own docs warn "accuracy drops as the state fills with
    irrelevant content -- filter before sending" (SYSTEM_ONE_MODELS.md),
    so sending a whole raw page as state would be a real accuracy
    regression for System One, not just a traditional-only optimization.

    Chrome headings are filtered out before the empty check, not after --
    a page whose only extracted heading is page furniture (a real
    production loss traced to exactly this: LESSONS_LEARNED.md #75/#79)
    must fall back to content the same way a page with zero headings at
    all already does, not be judged on furniture text alone.
    """
    headings = _strip_chrome_headings(extract_headings(content))
    state = headings if headings else content
    return state[:MAX_EMBED_CHARS]


class TraditionalRelevanceDecisionBackend(DecisionBackend):
    """Wraps score_headings' embedding-cosine-similarity math, completely
    unchanged, as a DecisionBackend -- picked from a real measurement
    (LESSONS_LEARNED.md #17), not something a generic method should
    silently replace. `state` here is already gate_state()'s output (the
    gate's shared state-prep step, not raw page content) -- embeds it and
    compares to the intent embedding directly, which is exactly what
    score_headings does once its own internal heading-extraction has
    already happened; extracting again here would be redundant, not
    incorrect, since gate_state() already ran it once upstream.
    """

    def __init__(self, intent_embedding: list[float], embed_fn: EmbedFn):
        self._intent_embedding = intent_embedding
        self._embed_fn = embed_fn

    async def noul(
        self, state: str, instructions: str = "", criteria: dict[str, str] | None = None,
    ) -> NoulAnswer:
        embedding = await self._embed_fn(state)
        value = cosine_similarity(self._intent_embedding, embedding)
        return NoulAnswer(value=value, method="traditional", raw={"cosine_similarity": value})


def make_score_fn(
    intent: str | None, intent_embedding: list[float] | None, embed_fn: EmbedFn,
) -> GateScoreFn:
    """intent_embedding=None means no intent was given -- the gate is off
    entirely, every page extracts, matching pipeline.py's ScoreFn shape
    (url, content) -> score with no special-casing needed in extract_worker.

    Decision method resolved once via decisions.get_backend() -- this
    module never reads config.USE_SYSTEM_ONE/SYSTEM_ONE_PROVIDER itself
    (see tests/test_decision_interface_ownership.py). `intent` (the raw
    string, not just its embedding) is needed here for System One's
    noul() instructions text -- Jev/Laya take natural-language questions,
    not vectors.
    """
    if intent_embedding is None:
        return _no_gate_score_fn

    traditional = TraditionalRelevanceDecisionBackend(intent_embedding, embed_fn)
    backend = get_backend(traditional)

    async def score_fn(url: str, content: str) -> float:
        state = gate_state(content)
        answer = await backend.noul(state, f"Is this page relevant to the intent: {intent!r}?")
        return answer.value

    return score_fn
