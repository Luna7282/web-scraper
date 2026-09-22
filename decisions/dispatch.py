"""get_backend(): the ONE place that reads config.USE_SYSTEM_ONE /
SYSTEM_ONE_PROVIDER to decide which concrete DecisionBackend answers --
every decision point has this called on its behalf by the composition
root (main.py), instead of reading that config itself. See
tests/test_decision_interface_ownership.py for the test that fails if a
decision-point module ever reads USE_SYSTEM_ONE directly.

Jev/Laya instances are cached at module level so multiple decision
points active in the same run share ONE JevBackend/LayaBackend --
critical for Laya specifically (step 3: "load once and keep it resident,
not per call"); reconstructing a Router per decision point would reload
the model repeatedly (7-10s each, per SYSTEM_ONE_MODELS.md).
"""
from __future__ import annotations

import os

import config
from decisions.backend import DecisionBackend
from decisions.jev import JevBackend
from decisions.laya import LayaBackend

_jev_singleton: JevBackend | None = None
_laya_singleton: LayaBackend | None = None


def get_backend(traditional: DecisionBackend) -> DecisionBackend:
    """traditional: this decision point's own existing/traditional
    implementation, already constructed by the caller -- it varies per
    decision point (embedding similarity for the relevance gate, the
    existing LLM prompt for branch selection, string similarity for
    dedup, LLMTraditionalBackend for sample quality). Returned as-is when
    USE_SYSTEM_ONE is false; swapped for the shared Jev/Laya singleton
    otherwise. Real call sites reach this only after
    config.validate_system_one_config() has already run (main.py's entry
    points) -- SYSTEM_ONE_PROVIDER is guaranteed valid and its
    key/package guaranteed present by then, so this does not re-validate.
    """
    if not config.USE_SYSTEM_ONE:
        return traditional

    global _jev_singleton, _laya_singleton
    if config.SYSTEM_ONE_PROVIDER == config.SystemOneProvider.JEV.value:
        if _jev_singleton is None:
            _jev_singleton = JevBackend(api_key=os.getenv("TYPESAFE_API_KEY"))
        return _jev_singleton
    if config.SYSTEM_ONE_PROVIDER == config.SystemOneProvider.LAYA.value:
        if _laya_singleton is None:
            _laya_singleton = LayaBackend()
        return _laya_singleton
    raise AssertionError(
        f"unreachable: config.validate_system_one_config() should already have "
        f"rejected SYSTEM_ONE_PROVIDER={config.SYSTEM_ONE_PROVIDER!r}"
    )
