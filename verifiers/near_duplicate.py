"""near_duplicate@1: within one run and one record type, a row whose key
text is the same as, or nearly the same as, an earlier row's fails.

  - Key text: qa -> question; code_example -> instruction + code; other
    types -> question / instruction if present (else not applicable).
    Normalised with chunk_store.normalize_chunk_text (the export dedup's
    rule), then compared exactly and by token-set (Jaccard) similarity >=
    the threshold (option "threshold", default config.NEAR_DUPLICATE_THRESHOLD).
  - ORDER-DEPENDENT, by design (Step 0 decision 4): the first row seen
    passes and later ones fail. Within a page that is the earliest row in
    chunk order; across pages it is the order pages finish extraction,
    which varies between runs. "Seen" = rows actually written (after
    balancing), plus earlier rows of the page being verified; on resume,
    rows already in canonical.jsonl.
"""

from __future__ import annotations

import re
from collections import defaultdict

import config
from storage.chunk_store import normalize_chunk_text
from verifiers import PageState, Verdict, Verifier

_TOKEN = re.compile(r"\w+")


def key_text(row: dict) -> str | None:
    if row.get("record_type") == "code_example":
        return f"{row.get('instruction', '')}\n{row.get('code', '')}"
    value = row.get("question") or row.get("instruction")
    return value if isinstance(value, str) else None


def tokens(text: str) -> frozenset:
    return frozenset(_TOKEN.findall(normalize_chunk_text(text)))


class NearDuplicateV1(Verifier):
    name, version = "near_duplicate", 1

    def __init__(self, options: dict | None = None):
        options = dict(options or {})
        threshold = options.pop("threshold", config.NEAR_DUPLICATE_THRESHOLD)
        if options:
            raise ValueError(f"verifier {self.id} options: only 'threshold' is allowed, got {sorted(options)}")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold <= 1:
            raise ValueError(f"verifier {self.id} threshold must be a number in (0, 1]")
        self.threshold = float(threshold)
        self._exact: dict[tuple, str] = {}                 # (type, normalised text) -> where
        self._by_size: dict[tuple, list] = defaultdict(list)  # (type, token count) -> [(tokens, where)]

    def applies(self, row: dict) -> bool:
        return key_text(row) is not None

    def _match(self, rtype: str, text: str, extra: list[dict]) -> tuple[float, str] | None:
        norm = normalize_chunk_text(text)
        if (rtype, norm) in self._exact:
            return 1.0, self._exact[(rtype, norm)]
        toks = tokens(text)
        candidates = []
        n = len(toks)
        lo, hi = int(n * self.threshold), int(n / self.threshold) + 1 if self.threshold else n
        for size in range(lo, hi + 1):   # Jaccard >= t needs the sizes within a factor t
            candidates += self._by_size.get((rtype, size), [])
        for row in extra:
            if row.get("record_type") == rtype and key_text(row) is not None:
                candidates.append((tokens(key_text(row)), _where(row)))
        best = None
        for other, where in candidates:
            if not toks and not other:
                sim = 1.0
            elif not toks or not other:
                continue
            else:
                sim = len(toks & other) / len(toks | other)
            if sim >= self.threshold and (best is None or sim > best[0]):
                best = (sim, where)
        return best

    async def verify(self, row: dict, page: PageState) -> Verdict:
        hit = self._match(row.get("record_type"), key_text(row), page.kept)
        if hit is None:
            return Verdict.ok()
        sim, where = hit
        return Verdict.fail(f"near-duplicate of an earlier row ({where}, similarity {sim:.2f})")

    def commit(self, rows: list[dict]) -> None:
        for row in rows:
            text = key_text(row)
            if text is None:
                continue
            rtype = row.get("record_type")
            self._exact.setdefault((rtype, normalize_chunk_text(text)), _where(row))
            toks = tokens(text)
            self._by_size[(rtype, len(toks))].append((toks, _where(row)))


def _where(row: dict) -> str:
    return f"{row.get('source_url')} chunk {row.get('chunk_index')}"
