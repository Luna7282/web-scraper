"""Verifier hook (headless contract v2, Brief 3B Step 5, item 13).

A verifier takes one row and returns a Verdict: pass / fail / unknown, with
reasons, under its own name and version. Verifiers run in the v2 extract
path after generation and BEFORE balancing and the writer, so caps count
only rows that survived. Config:

    "verifiers": [{"name": "schema", "version": 1, "on_fail": "flag"},
                  {"name": "external", "version": 1, "on_fail": "drop",
                   "options": {"command": ["/path/to/check"], "timeout_seconds": 30,
                               "record_types": ["code_example"]}}]

(default: schema@1, flag). on_fail "drop" removes a row that FAILS that
verifier (unknown never drops); "flag" keeps it. Every row that is written
carries `verification` (one entry per verifier that applied, in config
order) and `verification_status`: fail if any failed, else unknown if any
was unknown, else pass; "unverified" when no verifier applied.

Shipped: schema@1, near_duplicate@1, code_syntax@1 (generic; never execute
anything) and external@1 (a command you supply; see verifiers/external.py --
an external verifier that runs generated code must sandbox it itself).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field

VERDICTS = ("pass", "fail", "unknown")
ON_FAIL = ("drop", "flag")
DEFAULT_SPECS = ({"name": "schema", "version": 1, "on_fail": "flag"},)


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reasons: tuple[str, ...] = ()

    @classmethod
    def ok(cls) -> Verdict:
        return cls("pass")

    @classmethod
    def fail(cls, *reasons: str) -> Verdict:
        return cls("fail", tuple(reasons))

    @classmethod
    def unknown(cls, *reasons: str) -> Verdict:
        return cls("unknown", tuple(reasons))


@dataclass
class PageState:
    """What a verifier may know about the page being verified: the rows of
    this page that already passed through (in chunk order) and weren't dropped."""
    url: str
    kept: list[dict] = field(default_factory=list)


class Verifier:
    name: str = ""
    version: int = 0
    record_types: tuple[str, ...] | None = None    # None = every record type
    external: bool = False                          # runs concurrently, bounded by the run's setting

    def __init__(self, options: dict | None = None):
        if options:
            raise ValueError(f"verifier {self.id} takes no options, got {sorted(options)}")

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    def applies(self, row: dict) -> bool:
        return self.record_types is None or row.get("record_type") in self.record_types

    async def verify(self, row: dict, page: PageState) -> Verdict:
        raise NotImplementedError

    def commit(self, rows: list[dict]) -> None:
        """Rows that were finally written (after balancing). Default: nothing."""


def _registry() -> dict:
    from verifiers.code_syntax import CodeSyntaxV1
    from verifiers.external import ExternalV1
    from verifiers.near_duplicate import NearDuplicateV1
    from verifiers.schema import SchemaV1
    return {(v.name, v.version): v for v in (SchemaV1, NearDuplicateV1, CodeSyntaxV1, ExternalV1)}


REGISTRY_IDS = ("schema@1", "near_duplicate@1", "code_syntax@1", "external@1")


def validate_specs(specs) -> None:
    """Shape and registry check for a config's `verifiers` (ValueError)."""
    if not isinstance(specs, list):
        msg = "verifiers must be a list of {name, version, on_fail?, options?}"
        raise ValueError(msg)  # noqa: TRY004 -- a config error, reported as such
    reg = _registry()
    seen = set()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) - {"name", "version", "on_fail", "options"} \
                or "name" not in spec or "version" not in spec:
            raise ValueError("each verifier must be {name, version, on_fail?, options?}")
        key = (spec["name"], spec["version"])
        if type(spec["version"]) is not int or key not in reg:
            raise ValueError(f"unknown verifier {spec['name']!r} version {spec['version']!r}; "
                             f"available: {', '.join(REGISTRY_IDS)}")
        if spec.get("on_fail", "flag") not in ON_FAIL:
            raise ValueError(f"verifier {spec['name']}@{spec['version']} on_fail must be one of {list(ON_FAIL)}")
        if not isinstance(spec.get("options", {}), dict):
            msg = f"verifier {spec['name']}@{spec['version']} options must be an object"
            raise ValueError(msg)  # noqa: TRY004 -- a config error, reported as such
        ident = (key, repr(sorted(spec.get("options", {}).items())))
        if ident in seen:
            raise ValueError(f"verifier {spec['name']}@{spec['version']} listed twice with the same options")
        seen.add(ident)
        reg[key](spec.get("options"))  # options check


def build(specs=None) -> list[tuple[Verifier, str]]:
    reg = _registry()
    return [(reg[(s["name"], s["version"])](s.get("options")), s.get("on_fail", "flag"))
            for s in (DEFAULT_SPECS if specs is None else specs)]


class VerifierRunner:
    """One per v2 run. run() before balancing, commit() after it."""

    def __init__(self, entries: list[tuple[Verifier, str]], external_concurrency: int = 1):
        self.entries = entries
        self._external_sem = asyncio.Semaphore(max(1, external_concurrency))
        self.stats = {i: Counter() for i in range(len(entries))}
        self.rows_checked = 0
        self.rows_dropped = 0
        self.status_counts: Counter = Counter()

    def seed_written(self, rows: list[dict]) -> None:
        """Resume: rows already written count as seen (near_duplicate)."""
        self.commit(rows)

    async def _safe(self, verifier: Verifier, row: dict, page: PageState) -> Verdict:
        try:
            if verifier.external:
                async with self._external_sem:
                    v = await verifier.verify(row, page)
            else:
                v = await verifier.verify(row, page)
        except Exception as e:  # noqa: BLE001 -- a broken verifier never fails the page
            return Verdict.unknown(f"verifier error: {type(e).__name__}")
        return v if v.verdict in VERDICTS else Verdict.unknown(f"invalid verdict {v.verdict!r}")

    async def run(self, url: str, rows: list[dict]) -> list[dict]:
        if not self.entries:
            return rows
        page = PageState(url)
        # External verifiers are independent of order: run them concurrently first.
        external: dict[tuple[int, int], Verdict] = {}
        jobs = [(ri, vi) for ri, row in enumerate(rows) for vi, (v, _) in enumerate(self.entries)
                if v.external and v.applies(row)]
        if jobs:
            verdicts = await asyncio.gather(*(self._safe(self.entries[vi][0], rows[ri], page) for ri, vi in jobs))
            external = dict(zip(jobs, verdicts))
        kept = []
        for ri, row in enumerate(rows):   # in chunk order: the earliest of a page's near-duplicates wins
            results, drop = [], False
            for vi, (verifier, on_fail) in enumerate(self.entries):
                if not verifier.applies(row):
                    continue
                v = external[(ri, vi)] if verifier.external else await self._safe(verifier, row, page)
                self.stats[vi][v.verdict] += 1
                if v.verdict == "fail" and on_fail == "drop":
                    self.stats[vi]["dropped"] += 1
                    drop = True
                results.append({"verifier": verifier.name, "version": verifier.version, "verdict": v.verdict,
                                "reasons": list(v.reasons)})
            self.rows_checked += 1
            status = ("unverified" if not results else "fail" if any(r["verdict"] == "fail" for r in results)
                      else "unknown" if any(r["verdict"] == "unknown" for r in results) else "pass")
            if drop:
                self.rows_dropped += 1
                self.status_counts["dropped"] += 1
                continue
            row["verification"] = results
            row["verification_status"] = status
            self.status_counts[status] += 1
            page.kept.append(row)
            kept.append(row)
        return kept

    def commit(self, rows: list[dict]) -> None:
        for verifier, _ in self.entries:
            verifier.commit(rows)

    def report(self) -> dict:
        """result.json's `verification` section."""
        return {
            "verifiers": [{"verifier": v.name, "version": v.version, "on_fail": on_fail,
                           **{k: self.stats[i][k] for k in (*VERDICTS, "dropped")}}
                          for i, (v, on_fail) in enumerate(self.entries)],
            "rows_checked": self.rows_checked,
            "rows_dropped": self.rows_dropped,
            "written_rows_by_status": {k: self.status_counts[k] for k in ("pass", "fail", "unknown", "unverified")},
        }
