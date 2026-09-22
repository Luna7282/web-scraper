"""Structural verification that decision-point modules never read
config.USE_SYSTEM_ONE/SYSTEM_ONE_PROVIDER directly -- decisions/dispatch.py
(get_backend()) is the one sanctioned place, per step 3's "every decision
point calls the interface; no call site reads the env vars itself".
A future change that adds a hardcoded traditional-vs-System-One branch
straight into a decision point (bypassing get_backend()) fails this test
instead of silently reintroducing exactly the thing this interface exists
to prevent -- same discipline as test_writer_ownership.py, source-text
verification instead of a one-time assumption.
"""
import ast
import inspect
import unittest

import crawl.branch_select as branch_select
import content.relevance as relevance
import content.sample_quality as sample_quality
import crawl.pipeline as pipeline
import export.export_formats as export_formats
import decisions.dispatch as dispatch

_FORBIDDEN_NAMES = {"USE_SYSTEM_ONE", "SYSTEM_ONE_PROVIDER", "SystemOneProvider"}

DECISION_POINT_MODULES = [branch_select, relevance, sample_quality, pipeline, export_formats]


def _real_code_references_forbidden_name(module) -> str | None:
    """AST-based, not a source-text grep -- a docstring that merely
    *explains* "this module never reads config.USE_SYSTEM_ONE" (as several
    of these modules' own docstrings do, by design) must not trip this
    check; only an actual Attribute/Name node in real code should."""
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            return node.attr
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            return node.id
    return None


class TestDecisionInterfaceOwnership(unittest.TestCase):
    def test_decision_point_modules_never_read_system_one_config_directly(self):
        for module in DECISION_POINT_MODULES:
            found = _real_code_references_forbidden_name(module)
            self.assertIsNone(
                found, f"{module.__name__} reads System One config directly "
                f"({found!r}) -- route through decisions.get_backend() instead",
            )

    def test_dispatch_module_is_the_one_place_that_does(self):
        # The inverse check: get_backend() itself MUST reference these --
        # if this ever stops matching, the ownership test above would
        # start passing vacuously (nobody reads it anywhere), which is a
        # different bug (System One silently never engages) masquerading
        # as a clean result.
        self.assertIsNotNone(_real_code_references_forbidden_name(dispatch))


if __name__ == "__main__":
    unittest.main()
