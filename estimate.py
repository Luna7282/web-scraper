"""Dry-pass estimate (contract v2 `dry_run: true`, Brief 3B Step 2).

The dry pass fetches and chunks pages exactly as a real run would -- same
fetch, same relevance gate, same extraction_units_fn (the real chunker, not
article text: Brief 2c's estimate was 2.5x low for that reason) -- and makes
zero LLM calls. This module turns what it saw into estimate.json.

Per call, input = the generator's system prompt + "Text to process:\\n\\n" +
its request text capped at config.MAX_EXTRACT_CHARS (what the worker's LLM
call sends). Calls are counted per generator, from each one's wants().
Tokens use the measured basis in config (ESTIMATE_*); output is weighted
by the measured output/input ratio. Quality-scoring calls depend on how many
pairs come back, so they are reported as not estimable.
"""

from __future__ import annotations

import config
from generators import USER_PREFIX


def build_estimate(pages: dict, *, provider: str, model: str, generators: list, provider_preflight: bool,
                   quality_scoring: bool) -> dict:
    """pages: url -> {"units": [unit char counts], "calls": {generator id:
    [(system prompt chars, request text chars), ...]}, ...} or {"skipped":
    reason}. generators: the run's Generator objects, in config order."""
    units_per_page = {u: len(p.get("units", [])) for u, p in sorted(pages.items())}
    total_units = sum(units_per_page.values())
    per_gen = []
    for gen in generators:
        calls = [c for p in pages.values() for c in p.get("calls", {}).get(gen.id, [])]
        chars = sum(sys_len + len(USER_PREFIX) + min(text_len, config.MAX_EXTRACT_CHARS) for sys_len, text_len in calls)
        tokens_in = round(chars / config.ESTIMATE_CHARS_PER_INPUT_TOKEN)
        per_gen.append({
            **gen.spec(), "calls": len(calls), "input_chars": chars, "input_tokens": tokens_in,
            "output_tokens": round(tokens_in * config.ESTIMATE_OUTPUT_PER_INPUT_TOKEN),
            "calls_per_page": {u: len(p.get("calls", {}).get(gen.id, [])) for u, p in sorted(pages.items())
                               if "units" in p},
            "output_ratio": "measured (qa prompt)" if gen.id == "qa@1" else "unmeasured: assumed the qa ratio",
        })
    extraction = sum(g["calls"] for g in per_gen)
    input_chars = sum(g["input_chars"] for g in per_gen)
    input_tokens = round(input_chars / config.ESTIMATE_CHARS_PER_INPUT_TOKEN)
    calls = {"extraction": extraction, "preflight": 1 if provider_preflight else 0,
             "quality_scoring": "not estimable (depends on pairs returned)" if quality_scoring else 0}
    calls["total_known"] = calls["extraction"] + calls["preflight"]
    return {
        "provider": provider, "model": model, "generators": per_gen,
        "pages": len(pages), "pages_skipped": {u: p["skipped"] for u, p in sorted(pages.items()) if "skipped" in p},
        "units_per_page": units_per_page, "total_units": total_units, "expected_calls": calls,
        "input_chars": input_chars,
        # Fingerprints of what was chunked, to tell "the page changed" from
        # "the estimate is wrong" when a later real run differs.
        "page_fingerprints": {u: {"content_sha256": p.get("content_sha256"), "unit_chars": p["units"],
                                  "unit_sha256": p.get("unit_sha256", [])}
                              for u, p in sorted(pages.items()) if "units" in p},
        "token_estimate": {"input_tokens": input_tokens,
                           "output_tokens": round(input_tokens * config.ESTIMATE_OUTPUT_PER_INPUT_TOKEN),
                           "chars_per_input_token": config.ESTIMATE_CHARS_PER_INPUT_TOKEN,
                           "output_per_input_token": config.ESTIMATE_OUTPUT_PER_INPUT_TOKEN,
                           "basis": config.ESTIMATE_BASIS},
    }
