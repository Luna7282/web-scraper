"""Answer shapes every DecisionBackend returns, regardless of which
concrete backend answered -- a call site reads .value/.choice/.score the
same way whether traditional, Jev, or Laya produced it. `method` and
`raw` exist specifically for Step 4's traceability requirement (record
which decision method produced each decision); `reason` is populated by
the traditional (LLM) backend and left None by Jev/Laya, which are
non-generative and cannot produce free-text reasoning -- see
SYSTEM_ONE_MODELS.md. A caller that displays `reason` to a user should
handle None explicitly (e.g. "no reasoning available under System One"),
not treat its absence as a bug.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NoulAnswer:
    value: float  # P(true), 0.0-1.0
    method: str  # "traditional" | "jev" | "laya"
    confidence: float | None = None
    reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    method: str
    confidence: float | None = None
    reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    method: str
    confidence: float | None = None
    reason: str | None = None
    raw: dict = field(default_factory=dict)
