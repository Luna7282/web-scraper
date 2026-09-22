"""LayaBackend: Convai Innovations' open-weights System One model, run
locally via the Router (auto language/script detection -- see
SYSTEM_ONE_MODELS.md's Honest-Limits #4: the English-only root checkpoint
silently collapses on non-Latin script, so this project always goes
through the Router, never loads a single checkpoint directly). `laya`
itself is imported lazily, inside __init__, not at module import time --
`decisions` as a package must import cleanly whether or not `laya` is
installed (config.py::validate_system_one_config() is the actual gate
that decides whether SYSTEM_ONE_PROVIDER=laya is even allowed to reach
this class).
"""
from __future__ import annotations

import asyncio
import os

from decisions.backend import DecisionBackend, DecisionError
from decisions.types import NoulAnswer


class LayaBackend(DecisionBackend):
    def __init__(self):
        # Per Laya's own docs: prevents a documented TensorFlow-import
        # deadlock during model construction. Set before import, not
        # after -- the deadlock is triggered by the import itself.
        os.environ.setdefault("USE_TF", "0")
        try:
            import laya
        except ImportError as e:
            raise DecisionError(
                "laya is not installed -- config.py::validate_system_one_config() "
                "should have caught this at startup; see SYSTEM_ONE_MODELS.md "
                "before installing it."
            ) from e
        # preload=True: load once, keep resident (step 3 instruction) --
        # Router(max_loaded=1)'s per-switch reload (7-10s) is never
        # acceptable in a live pipeline. One Router per process, shared
        # across every decision point via decisions/dispatch.py's
        # singleton cache -- not one per decision point.
        self._router = laya.Router(preload=True)

    async def noul(
        self, state: str, instructions: str, criteria: dict[str, str] | None = None,
    ) -> NoulAnswer:
        question = {"type": "noul", "instructions": instructions}
        if criteria:
            question["criteria"] = criteria
        try:
            # Router.predict() is a synchronous, GPU/CPU-bound forward pass
            # -- blocking I/O invariant applies exactly as it does to any
            # other synchronous call inside an async def (CLAUDE.md's event
            # loop section): must not run directly on the shared loop.
            result = await asyncio.to_thread(self._router.predict, state, {"_": question})
        except Exception as e:
            raise DecisionError(f"Laya predict() failed: {type(e).__name__}: {e}") from e
        answer = result["answers"]["_"]
        return NoulAnswer(value=answer["noul"], method="laya", raw=answer)
