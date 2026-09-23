"""LLM-assisted branch selection over a completed --map path-tree
projection: classify each branch (select/reject/uncertain, one reason
each) against the user's intent. This proposes only -- nothing here
mutates a frontier.db; the caller still has to run --select/
--select-prefix (or the UI's own action) to actually promote anything.
Wrong branch choices cost hours of wasted crawling, so this must never
auto-apply.

Branch extraction (extract_branches) is pure and offline-testable, same
discipline as crawl/scope.py -- no site-specific logic, no I/O.

Decision method is now the decisions/ interface (traditional/Jev/Laya),
resolved once per call via decisions.get_backend() -- this module never
reads config.USE_SYSTEM_ONE/SYSTEM_ONE_PROVIDER itself (see
tests/test_decision_interface_ownership.py). Traditional's own
implementation (TraditionalBranchDecisionBackend, below) wraps the
EXACT prompt/parsing this module always used -- classify_fn (the
LLM call), build_classification_prompt, parse_branch_classification are
all unchanged from before this interface existed, so traditional's
behavior is bit-for-bit identical to today. A System One backend
(Jev/Laya) instead asks one noul question per branch, with that branch's
own sample URLs as its state -- "is this branch relevant to the intent?"
-- per this project's "prefer one yes/no question per item over one
large choice question" design rule; see suggest_branch_selection's
docstring for how that collapses back to select/reject/uncertain, and
why System One answers carry no free-text reason (they're
non-generative, see SYSTEM_ONE_MODELS.md).
"""
from __future__ import annotations

import json
import re
from typing import Awaitable, Callable

import config
from decisions import DecisionBackend, NoulAnswer, get_backend

ClassifyFn = Callable[[str], Awaitable[str]]

_DECISIONS = {"select", "reject", "uncertain"}
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_VALUE_BY_DECISION = {"select": 1.0, "reject": 0.0, "uncertain": 0.5}

BRANCH_CLASSIFICATION_SYSTEM_PROMPT = """You are helping a user decide which sections of a website to crawl for a specific purpose.

You will be given the user's intent and a list of branches discovered by mapping the site. Each branch is a URL path prefix, how many pages were found under it, and a handful of real sample URLs from it.

For each branch, decide exactly one of:
- "select": this branch's content is relevant to the intent and should be crawled.
- "reject": this branch's content is not relevant to the intent, or is low-value regardless of intent (changelogs, release notes, author/tag/index listing pages, contribution guides), and should be skipped.
- "uncertain": you genuinely cannot tell from the branch name, page count, and sample URLs alone. Use this rather than guessing -- a wrong guess costs the user hours of wasted crawling; an honest "uncertain" costs one glance.

Weigh the sample URLs more heavily than the branch name -- a branch's name alone (e.g. "reference") is often uninformative, but its real URLs usually reveal what's actually there.

Respond with a JSON array, one object per branch, in the same order given, each with exactly these keys: "prefix" (copied exactly from the input), "decision" (one of "select"/"reject"/"uncertain"), and "reason" (one sentence, specific to what you saw in that branch's sample URLs -- not a generic restatement of the decision)."""


def extract_branches(tree: dict, max_samples: int = 5) -> list[dict]:
    """Branches = the children of the path tree's effective root: the
    first point the structure actually forks. Segments above that
    (locale/version prefixes like en/stable/) are skipped by descending
    through any node that has exactly one child and no page of its own --
    there is nothing to select/reject at that level other than
    "everything". Confirmed against a real docs.manim.community map: the
    tree's literal root has one child ("en"), which has one child
    ("stable"), and only stable's children (reference/, changelog/,
    tutorials/, ...) actually diverge -- this walk lands there without
    the caller needing to know that path prefix in advance.

    Each branch: {"prefix": full path, "page_count": pages anywhere in
    this subtree, "sample_urls": up to max_samples of them, sorted for a
    stable, reviewable order}. A branch entirely made of leaf pages (a
    blog's individual posts, say) still comes back as one branch with
    page_count=1 -- this function doesn't decide what's "worth" deciding
    on, it reports the tree as found.
    """
    root = tree
    while len(root["children"]) == 1 and root["url_node"] is None:
        root = next(iter(root["children"].values()))

    branches = []
    for child in root["children"].values():
        urls = sorted(_collect_urls(child))
        branches.append({
            "prefix": child["path"],
            "page_count": len(urls),
            "sample_urls": urls[:max_samples],
        })
    return branches


def _collect_urls(node: dict) -> list[str]:
    urls = [node["url_node"]["url"]] if node["url_node"] is not None else []
    for child in node["children"].values():
        urls.extend(_collect_urls(child))
    return urls


def build_classification_prompt(branches: list[dict], intent: str) -> str:
    lines = [f"User's intent: {intent}", "", "Branches:"]
    for b in branches:
        lines.append(f"\n- prefix: {b['prefix']}")
        lines.append(f"  page_count: {b['page_count']}")
        lines.append("  sample_urls:")
        for u in b["sample_urls"]:
            lines.append(f"    - {u}")
    return "\n".join(lines)


class MalformedClassificationError(Exception):
    pass


def parse_branch_classification(raw_text: str, branches: list[dict]) -> list[dict]:
    """Merges the model's decision+reason back onto the known branch dicts
    by prefix, returned in `branches`' own order -- never the model's
    order, so callers always see one stable sequence. A branch the model
    didn't return, or returned with a decision outside the fixed set, gets
    decision="uncertain" and an explicit reason saying so, rather than
    being silently dropped: a dropped branch is the same failure mode as
    an auto-selected one -- it disappears from the user's review instead
    of asking for a look.
    """
    candidates = [raw_text.strip()]
    fenced = _FENCE_RE.sub("", raw_text).strip()
    if fenced != candidates[0]:
        candidates.append(fenced)
    start, end = raw_text.find("["), raw_text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw_text[start:end + 1])

    parsed = None
    for candidate in candidates:
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(result, list):
            parsed = result
            break

    if parsed is None:
        raise MalformedClassificationError(
            f"could not parse a JSON array from response: {raw_text[:200]!r}"
        )

    by_prefix = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        prefix, decision = item.get("prefix"), item.get("decision")
        if prefix is None or decision not in _DECISIONS:
            continue
        by_prefix[prefix] = {"decision": decision, "reason": item.get("reason", "")}

    results = []
    for b in branches:
        match = by_prefix.get(b["prefix"])
        if match is None:
            match = {"decision": "uncertain", "reason": "model did not return a classification for this branch"}
        results.append({**b, **match})
    return results


class TraditionalBranchDecisionBackend(DecisionBackend):
    """Wraps the existing branch-classification prompt/parsing (classify_fn
    + build_classification_prompt + parse_branch_classification),
    completely unchanged, as a DecisionBackend -- traditional's
    branch-selection behavior is bit-for-bit what it was before this
    interface existed. Captures `branches`/`intent` directly rather than
    reconstructing them from noul_many()'s generic (state, instructions)
    per-item shape: the old prompt's exact shape (page_count + every
    sample URL, all branches in one message) is richer than that generic
    split, and reconstructing it from there would risk feeding the LLM
    subtly different information -- not a behavior-preserving refactor.
    Only noul_many() is meaningful here (one call for every branch, the
    documented cost property); single-item noul() is intentionally
    unsupported.
    """

    def __init__(self, classify_fn: ClassifyFn, branches: list[dict], intent: str):
        self._classify_fn = classify_fn
        self._branches = branches
        self._intent = intent

    async def noul(self, state, instructions, criteria=None) -> NoulAnswer:
        raise NotImplementedError(
            "TraditionalBranchDecisionBackend only supports noul_many "
            "(one batched call covering every branch)"
        )

    async def noul_many(self, items: dict) -> dict[str, NoulAnswer]:
        raw = await self._classify_fn(build_classification_prompt(self._branches, self._intent))
        classified = parse_branch_classification(raw, self._branches)
        return {
            b["prefix"]: NoulAnswer(
                value=_VALUE_BY_DECISION[b["decision"]], method="traditional", reason=b["reason"], raw=b,
            )
            for b in classified
        }


def _value_to_decision(value: float, method: str) -> str:
    thresholds = config.DECISION_THRESHOLDS[("branch_selection", method)]
    if value >= thresholds["select"]:
        return "select"
    if value <= thresholds["reject"]:
        return "reject"
    return "uncertain"


async def suggest_branch_selection(
    tree: dict, intent: str, classify_fn: ClassifyFn, max_samples: int = 5
) -> list[dict]:
    """Traditional (default): one LLM call, every branch, exactly as
    before -- see TraditionalBranchDecisionBackend. System One (Jev/Laya,
    via decisions.get_backend()): one noul call per branch, each branch's
    own sample URLs as its state -- "is this branch relevant to the
    intent?" -- collapsed back to select/reject/uncertain via
    config.DECISION_THRESHOLDS[("branch_selection", method)], looked up
    per the answering backend's own method (not one shared number across
    traditional/jev/laya -- see config.py's DECISION_THRESHOLDS docstring).
    Jev/Laya
    don't generate free-text reasoning (non-generative models -- see
    SYSTEM_ONE_MODELS.md), so `reason` for those is a restatement of the
    raw probability, not real reasoning -- said explicitly, not left
    blank and unexplained. Every result also carries `decision_method`
    ("traditional"/"jev"/"laya") for traceability.
    """
    branches = extract_branches(tree, max_samples=max_samples)
    if not branches:
        return []

    traditional = TraditionalBranchDecisionBackend(classify_fn, branches, intent)
    backend = get_backend(traditional)

    items = {
        b["prefix"]: (
            "\n".join(b["sample_urls"]) or "(no sample URLs found under this branch)",
            f"This branch has {b['page_count']} page(s) under it. Is it relevant to "
            f"the user's intent: {intent!r}? Weigh the sample URLs more than the branch name.",
            None,
        )
        for b in branches
    }
    answers = await backend.noul_many(items)

    results = []
    for b in branches:
        answer = answers[b["prefix"]]
        decision = _value_to_decision(answer.value, answer.method)
        reason = answer.reason
        if reason is None:
            reason = (
                f"System One ({answer.method}): P(relevant)={answer.value:.2f} -- "
                f"no free-text reasoning available from this backend, see SYSTEM_ONE_MODELS.md"
            )
        results.append({**b, "decision": decision, "reason": reason, "decision_method": answer.method})
    return results
