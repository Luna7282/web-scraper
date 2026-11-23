"""Per-run cost and token accounting (contract v2, Brief 3B Step 2, item 20).

One bucket per (provider, model, role, generator) -- role: extraction /
quality / preflight; generator: the v2 generator id ("qa@1") for extraction
calls made through a generator, else None (and left out of the row). Counts every call attempt (a breaker retry is another call) by
outcome, and token totals from the calls that reported usage. A token total is
null when no call reported it -- never assumed 0 -- and usage_missing_calls
says how many calls didn't report.
"""

from __future__ import annotations

from dataclasses import dataclass

OUTCOMES = ("ok", "malformed", "rate_limited", "unavailable", "error")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens")


@dataclass
class _Bucket:
    calls: int = 0
    usage_reported_calls: int = 0
    unavailable_releases: int = 0

    def __post_init__(self):
        self.outcomes = dict.fromkeys(OUTCOMES, 0)
        self.tokens = dict.fromkeys(TOKEN_FIELDS)

    def to_dict(self) -> dict:
        return {"calls": self.calls, **self.outcomes, "unavailable_releases": self.unavailable_releases,
                **self.tokens, "usage_reported_calls": self.usage_reported_calls,
                "usage_missing_calls": self.calls - self.usage_reported_calls}


class CostLedger:
    def __init__(self, provider: str, model: str):
        self.provider, self.model = provider, model
        self._buckets: dict[tuple[str, str, str, str], _Bucket] = {}

    def _bucket(self, role: str, generator: str | None = None) -> _Bucket:
        return self._buckets.setdefault((self.provider, self.model, role, generator or ""), _Bucket())

    def record(self, role: str, outcome: str, usage: dict | None = None, generator: str | None = None) -> None:
        if outcome not in OUTCOMES:
            outcome = "error"
        b = self._bucket(role, generator)
        b.calls += 1
        b.outcomes[outcome] += 1
        if usage:
            reported = False
            for field in TOKEN_FIELDS:
                value = usage.get(field)
                if isinstance(value, int):
                    b.tokens[field] = (b.tokens[field] or 0) + value
                    reported = reported or field in ("input_tokens", "output_tokens")
            b.usage_reported_calls += reported

    def unavailable_release(self, role: str = "extraction", generator: str | None = None) -> None:
        """A 5xx page release, booked against the generator whose call got the 5xx."""
        self._bucket(role, generator).unavailable_releases += 1

    def to_dict(self) -> dict:
        rows = [{"provider": p, "model": m, "role": r, **({"generator": g} if g else {}), **b.to_dict()}
                for (p, m, r, g), b in sorted(self._buckets.items())]
        totals = {"calls": sum(r["calls"] for r in rows)}
        for key in (*OUTCOMES, "unavailable_releases", "usage_reported_calls", "usage_missing_calls"):
            totals[key] = sum(r[key] for r in rows)
        for field in TOKEN_FIELDS:
            values = [r[field] for r in rows if r[field] is not None]
            totals[field] = sum(values) if values else None
        return {"by_model": rows, "totals": totals}
