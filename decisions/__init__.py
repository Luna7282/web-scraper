"""One decision interface, three implementations (traditional/jev/laya).

Every judgment point in the pipeline (branch selection, the relevance
gate, semantic dedup, sample quality scoring) calls a `DecisionBackend`'s
`noul`/`choice`/`score` -- never a raw LLM call, embedding comparison, or
string-similarity check of its own when that call IS the decision. Which
concrete backend answers is resolved in exactly one place
(`get_backend()`, called from the composition root -- main.py -- once per
run) and injected into each decision point the same way `extract_fn`/
`score_fn` already are; no decision-point module reads
`config.USE_SYSTEM_ONE`/`SYSTEM_ONE_PROVIDER` itself.

See SYSTEM_ONE_MODELS.md for what Jev/Laya actually are, and
tests/test_decision_interface_ownership.py for the test that fails if a
future change bypasses this and calls a provider directly from a
decision-point module.
"""
from decisions.types import NoulAnswer, ChoiceAnswer, ScoreAnswer
from decisions.backend import DecisionBackend, DecisionError
from decisions.llm_traditional import LLMTraditionalBackend
from decisions.jev import JevBackend
from decisions.laya import LayaBackend
from decisions.dispatch import get_backend

__all__ = [
    "NoulAnswer", "ChoiceAnswer", "ScoreAnswer",
    "DecisionBackend", "DecisionError",
    "LLMTraditionalBackend", "JevBackend", "LayaBackend",
    "get_backend",
]
