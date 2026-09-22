"""DecisionBackend: the one abstraction every decision point calls.

Single-item noul/choice/score are the abstract primitives -- they map
1:1 onto Jev's and Laya's real request shape (one state, one typed
question per call; see SYSTEM_ONE_MODELS.md). *_many are concrete
default implementations that just gather the single-item calls
concurrently; a backend that can genuinely batch many questions into one
network call (the traditional/LLM backend, which can pack many questions
into one chat completion) overrides them for efficiency. This is the
opposite of building *_many as the primitive and noul/choice/score as a
convenience wrapper -- it was tried that way first and rejected: forcing
every item into one shared "state" loses the per-item signal branch
selection depends on ("its sample URLs as state", not a state shared
across every branch in the call).
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from decisions.types import ChoiceAnswer, NoulAnswer, ScoreAnswer


class DecisionError(Exception):
    """A decision call failed outright. Never caught and silently turned
    into a fallback to a different method -- see CLAUDE.md's System One
    section: mixing methods in one dataset with no way to tell which rows
    came from which is worse than a loud failure. Callers (worker loops)
    are expected to let this propagate into their existing
    mark_extract_outcome(..., 'failed', reason)-style handling, exactly
    like any other exception from score_fn/extract_fn today."""


class DecisionBackend(ABC):
    @abstractmethod
    async def noul(
        self, state: str, instructions: str, criteria: dict[str, str] | None = None,
    ) -> NoulAnswer:
        """criteria: optional {"true": "...", "false": "..."} clarification
        of what true/false means, per Jev's noul schema. The only method
        every backend below actually implements -- all four decision
        points wired up today (branch selection, the relevance gate,
        semantic dedup, sample quality) are yes/no-shaped, per this
        project's own "prefer one yes/no question per item over one large
        choice question" design rule."""
        raise NotImplementedError

    # choice/score are part of the interface (Jev/Laya both support them
    # natively) but not required -- no decision point wired up today
    # needs anything but noul, and forcing every backend to implement
    # unused primitives is filler, not abstraction. Override when a real
    # caller needs one.
    async def choice(
        self, state: str, instructions: str, criteria: dict[str, str],
    ) -> ChoiceAnswer:
        """criteria: {option_key: description}."""
        raise NotImplementedError(f"{type(self).__name__} does not implement choice()")

    async def score(
        self, state: str, instructions: str, criteria: list[str],
    ) -> ScoreAnswer:
        """criteria: ordered spectrum levels, low to high."""
        raise NotImplementedError(f"{type(self).__name__} does not implement score()")

    # items: {key: (state, instructions, criteria)} -> {key: Answer}.
    # Default: concurrent individual calls -- correct for Jev/Laya, whose
    # whole design point is that one call is already cheap/fast, and for
    # any items whose "state" genuinely differs per item (which is the
    # common case here: each branch's own sample URLs, each Q&A pair's
    # own text -- not one shared document with several questions about
    # it). Override only where batching into fewer real network/compute
    # calls is actually worth it AND state can legitimately be shared or
    # folded into per-item instructions -- see LLMTraditionalBackend.
    async def noul_many(
        self, items: dict[str, tuple[str, str, dict[str, str] | None]],
    ) -> dict[str, NoulAnswer]:
        keys = list(items)
        answers = await asyncio.gather(*(self.noul(*items[k]) for k in keys))
        return dict(zip(keys, answers))

    async def choice_many(
        self, items: dict[str, tuple[str, str, dict[str, str]]],
    ) -> dict[str, ChoiceAnswer]:
        keys = list(items)
        answers = await asyncio.gather(*(self.choice(*items[k]) for k in keys))
        return dict(zip(keys, answers))

    async def score_many(
        self, items: dict[str, tuple[str, str, list[str]]],
    ) -> dict[str, ScoreAnswer]:
        keys = list(items)
        answers = await asyncio.gather(*(self.score(*items[k]) for k in keys))
        return dict(zip(keys, answers))
