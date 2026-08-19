"""Export CLI: turns a canonical.jsonl file (canonical.py, written once by
Writer during a crawl) into whatever training/eval shape a specific
framework wants (step 8 plan, Part B, levels 2-3).

Separate entry point from main.py on purpose -- changing output format
must never require re-crawling. Run as a module from the repo root (not
as a direct script -- the absolute `export.export_formats`-style imports
below need the repo root on sys.path, which only `-m` guarantees):

    uv run python -m export.export data/run/canonical.jsonl --schema alpaca --framework huggingface --out data/export/hf_alpaca

Cross-cutting steps always run in this order, regardless of
schema/framework: validate -> dedup -> split -> project -> package.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from export.export_formats import (
    ANSWER_NEAR_DUP_THRESHOLD,
    BATCH_PROJECTIONS,
    SCHEMA_PROJECTIONS,
    UNSUPPORTED_WITHOUT_EXTRA_PASS,
    dedup_by_question,
    semantic_dedup as semantic_dedup_pairs,
    split_records,
    to_raw_text,
    validate_records,
)
from crawl.scope import derive_prefix
from content.sectioning import disambiguate_slugs


def load_canonical_records(path: str) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _write_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_dataset_card(
    records: list[dict], *, intent: str | None, generation_model: str, min_answer_length: int,
    valid_count: int, rejected_count: int, dedup_removed: int, semantic_dedup_removed: int = 0,
) -> dict:
    """Provenance, not laundering: a tool that turns any site into training
    data should say where the data came from and under what conditions it
    was generated, not just hand over a JSONL file."""
    sites = sorted({r["source_url"].split("/")[2] for r in records if r.get("source_url")})
    crawl_dates = sorted({r.get("crawl_date") for r in records if r.get("crawl_date")})
    license_signals = sorted({r["license_signal"] for r in records if r.get("license_signal")})
    return {
        "sites": sites,
        "crawl_dates": crawl_dates,
        "intent": intent,
        "generation_model": generation_model,
        "row_counts": {
            "raw_canonical_records": valid_count + rejected_count,
            "valid_after_validation": valid_count,
            "rejected_by_validation": rejected_count,
            "removed_by_dedup": dedup_removed,
            "removed_by_semantic_dedup": semantic_dedup_removed,
            "final": valid_count - dedup_removed - semantic_dedup_removed,
        },
        "validation_min_answer_length": min_answer_length,
        "license_signals_observed": license_signals or None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# Verified against mlx-lm's own mlx_lm/LORA.md (github.com/ml-explore/
# mlx-lm), not guessed: the LoRA trainer auto-detects exactly four record
# shapes -- chat ("messages"), tool-calling ("messages" + "tools"),
# completions ("prompt"/"completion"), and plain "text" -- and explicitly
# has no built-in Alpaca support ("a dataset using only Alpaca-style keys
# would... fail automatic detection"). Only the schemas that produce one
# of those four shapes are accepted; found via a real export audit that
# package_mlx previously had no restriction at all and silently wrote
# files for alpaca/embedding_pairs/rag_eval/vertex that mlx-lm's loader
# can't read (LESSONS_LEARNED.md #49-#50, ROADMAP #34).
_MLX_KNOWN_SCHEMAS = {"conversational", "openai_finetune", "prompt_completion"}


def package_mlx(splits: dict[str, list[dict]], out_dir: str, project, schema: str) -> None:
    if schema not in _MLX_KNOWN_SCHEMAS:
        raise ValueError(
            f"mlx packaging has no verified mlx-lm-loadable shape for schema {schema!r} yet -- "
            f"known: {sorted(_MLX_KNOWN_SCHEMAS)}"
        )
    name_map = {"train": "train.jsonl", "validation": "valid.jsonl", "test": "test.jsonl"}
    for split_name, filename in name_map.items():
        _write_jsonl(os.path.join(out_dir, filename), [project(r) for r in splits.get(split_name, [])])


def package_huggingface(splits: dict[str, list[dict]], out_dir: str, project, dataset_card: dict) -> None:
    for split_name in ("train", "validation", "test"):
        _write_jsonl(os.path.join(out_dir, f"{split_name}.jsonl"), [project(r) for r in splits.get(split_name, [])])
    with open(os.path.join(out_dir, "dataset_card.json"), "w", encoding="utf-8") as f:
        json.dump(dataset_card, f, indent=2, ensure_ascii=False)


# Verified against LLaMA-Factory's own data/dataset_info.json (github.com/
# hiyouga/LLaMA-Factory), not guessed: a plain {instruction,input,output}
# JSON list needs no "columns" entry at all -- it already matches
# LLaMA-Factory's default alpaca field names. A {"messages": [...]} list
# needs "formatting": "sharegpt" plus an explicit role/content tag map --
# confirmed against real entries (ultrachat_200k, mllm_demo) that all
# specify role_tag/content_tag/user_tag/assistant_tag explicitly, not left
# implicit. Schemas with no known LLaMA-Factory convention refuse loudly
# rather than emit an unverified guess.
_LLAMA_FACTORY_KNOWN_SCHEMAS = {"alpaca", "conversational", "openai_finetune"}


def package_llama_factory(splits: dict[str, list[dict]], out_dir: str, project, schema: str,
                           dataset_name: str = "canonical_export") -> None:
    if schema not in _LLAMA_FACTORY_KNOWN_SCHEMAS:
        raise ValueError(
            f"llama-factory packaging has no verified dataset_info.json mapping for "
            f"schema {schema!r} yet -- known: {sorted(_LLAMA_FACTORY_KNOWN_SCHEMAS)}"
        )

    all_records = [r for rows in splits.values() for r in rows]
    filename = f"{dataset_name}.json"
    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
        json.dump([project(r) for r in all_records], f, indent=2, ensure_ascii=False)

    entry = {"file_name": filename}
    if schema in ("conversational", "openai_finetune"):
        entry["formatting"] = "sharegpt"
        entry["columns"] = {"messages": "messages"}
        entry["tags"] = {
            "role_tag": "role", "content_tag": "content",
            "user_tag": "user", "assistant_tag": "assistant",
        }
    # alpaca: no columns/formatting needed -- instruction/input/output
    # already match LLaMA-Factory's default.

    dataset_info = {dataset_name: entry}
    with open(os.path.join(out_dir, "dataset_info.json"), "w", encoding="utf-8") as f:
        json.dump(dataset_info, f, indent=2)


# Verified against Axolotl's own dataset-formats docs (docs.axolotl.ai),
# not guessed: "alpaca" is a real `type:` value for instruction/input/
# output JSON; a {"messages": [...]} chat-style dataset uses "chat_template",
# not the schema's own internal name. Axolotl also has a "completion" type
# and an "input_output" type that might fit prompt_completion, but their
# exact expected field names weren't confirmed against the docs -- left
# out rather than guessed. Anything without a verified mapping refuses
# rather than emitting an unverified `type:` value.
_AXOLOTL_TYPE_BY_SCHEMA = {
    "alpaca": "alpaca",
    "conversational": "chat_template",
    "openai_finetune": "chat_template",
}


def package_axolotl(out_dir: str, dataset_path: str, schema: str) -> None:
    if schema not in _AXOLOTL_TYPE_BY_SCHEMA:
        raise ValueError(
            f"axolotl packaging has no verified `type:` value for schema {schema!r} yet -- "
            f"known: {sorted(_AXOLOTL_TYPE_BY_SCHEMA)}"
        )
    axolotl_type = _AXOLOTL_TYPE_BY_SCHEMA[schema]
    yaml_stanza = f"datasets:\n  - path: {dataset_path}\n    type: {axolotl_type}\n"
    with open(os.path.join(out_dir, "axolotl_dataset.yaml"), "w", encoding="utf-8") as f:
        f.write(yaml_stanza)


def package_plain_jsonl(splits: dict[str, list[dict]], out_dir: str, project, section_depth: int) -> None:
    all_records = [r for rows in splits.values() for r in rows]
    _write_jsonl(os.path.join(out_dir, "unified.jsonl"), [project(r) for r in all_records])

    by_section: dict[str, list[dict]] = {}
    for r in all_records:
        by_section.setdefault(r.get("section", "root"), []).append(r)
    slugs = disambiguate_slugs(sorted(by_section.keys()))

    manifest = {}
    section_dir = os.path.join(out_dir, "sections")
    for section, rows in by_section.items():
        slug = slugs[section]
        _write_jsonl(os.path.join(section_dir, f"{slug}.jsonl"), [project(r) for r in rows])
        # derive_prefix assumes one host per call (true for every crawl this
        # project can currently run -- one root_url per run); a section
        # label that happened to collide across two different hosts in a
        # hypothetical multi-domain crawl would report only the first
        # URL's host here. Not a real case yet, not built around.
        urls = sorted({r["source_url"] for r in rows})
        prefix_result = derive_prefix(urls)
        url_prefix = f"https://{prefix_result.host}{prefix_result.prefix or '/'}"
        manifest[slug] = {"url_prefix": url_prefix, "row_count": len(rows)}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


FRAMEWORKS = ("mlx", "huggingface", "llama-factory", "axolotl", "plain-jsonl")


def run_export(
    canonical_path: str,
    out_dir: str,
    *,
    schema: str,
    framework: str,
    min_answer_length: int = 20,
    split_by: str = "section",
    seed: int = 42,
    intent: str | None = None,
    section_depth: int = 2,
    semantic_dedup: bool = False,
    semantic_dedup_threshold: float = ANSWER_NEAR_DUP_THRESHOLD,
    semantic_dedup_report: bool = False,
) -> dict:
    if schema in UNSUPPORTED_WITHOUT_EXTRA_PASS:
        raise ValueError(
            f"schema {schema!r} is not supported by this export pass: "
            f"{UNSUPPORTED_WITHOUT_EXTRA_PASS[schema]}"
        )
    if framework not in FRAMEWORKS:
        raise ValueError(f"unknown framework {framework!r}, expected one of {FRAMEWORKS}")

    records = load_canonical_records(canonical_path)
    valid, rejected = validate_records(records, min_answer_length=min_answer_length)
    deduped, dedup_removed = dedup_by_question(valid)

    os.makedirs(out_dir, exist_ok=True)

    # Runs after exact-question dedup, above -- catches rewordings that
    # stage demonstrably misses (LESSONS_LEARNED.md #33). Not applied by
    # default: semantic_dedup_report writes what WOULD be dropped at the
    # given threshold without touching `deduped`, precisely so the
    # threshold can be picked from a real report instead of guessed
    # before --semantic-dedup is ever turned on for a real export.
    semantic_removed = 0
    if semantic_dedup or semantic_dedup_report:
        kept, dropped_pairs = semantic_dedup_pairs(
            deduped, threshold=semantic_dedup_threshold, dry_run=not semantic_dedup,
        )
        if semantic_dedup_report:
            with open(os.path.join(out_dir, "semantic_dedup_report.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "threshold": semantic_dedup_threshold,
                        "applied": semantic_dedup,
                        "candidate_count": len(dropped_pairs),
                        "candidates": dropped_pairs,
                    },
                    f, indent=2, ensure_ascii=False,
                )
        if semantic_dedup:
            deduped = kept
            semantic_removed = len(dropped_pairs)

    generation_model = deduped[0]["generation_model"] if deduped else "unknown"
    card = build_dataset_card(
        records, intent=intent, generation_model=generation_model, min_answer_length=min_answer_length,
        valid_count=len(valid), rejected_count=len(rejected), dedup_removed=dedup_removed,
        semantic_dedup_removed=semantic_removed,
    )

    if schema in BATCH_PROJECTIONS:
        splits = split_records(deduped, by=split_by, seed=seed)
        batch_project = BATCH_PROJECTIONS[schema]
        for split_name, rows in splits.items():
            projected = batch_project(rows) if schema == "raw_text" else batch_project(rows, seed=seed)
            _write_jsonl(os.path.join(out_dir, f"{split_name}.jsonl"), projected)
        with open(os.path.join(out_dir, "dataset_card.json"), "w", encoding="utf-8") as f:
            json.dump(card, f, indent=2, ensure_ascii=False)
        return card

    project = SCHEMA_PROJECTIONS[schema]
    splits = split_records(deduped, by=split_by, seed=seed)

    if framework == "mlx":
        package_mlx(splits, out_dir, project, schema)
    elif framework == "huggingface":
        package_huggingface(splits, out_dir, project, card)
    elif framework == "llama-factory":
        package_llama_factory(splits, out_dir, project, schema)
    elif framework == "axolotl":
        all_path = os.path.join(out_dir, "dataset.jsonl")
        _write_jsonl(all_path, [project(r) for rows in splits.values() for r in rows])
        package_axolotl(out_dir, all_path, schema)
    elif framework == "plain-jsonl":
        package_plain_jsonl(splits, out_dir, project, section_depth)

    with open(os.path.join(out_dir, "dataset_card.json"), "w", encoding="utf-8") as f:
        json.dump(card, f, indent=2, ensure_ascii=False)
    return card


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("canonical_path", help="Path to canonical.jsonl written by a crawl.")
    parser.add_argument("--out", required=True, help="Output directory.")
    parser.add_argument(
        "--schema", required=True,
        choices=list(SCHEMA_PROJECTIONS) + list(BATCH_PROJECTIONS) + list(UNSUPPORTED_WITHOUT_EXTRA_PASS),
    )
    parser.add_argument("--framework", default="plain-jsonl", choices=FRAMEWORKS)
    parser.add_argument("--min-answer-length", type=int, default=20)
    parser.add_argument("--split-by", default="section", choices=["section", "source_url"])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--intent", default=None, help="Intent string used for the crawl, recorded on the dataset card.")
    parser.add_argument("--section-depth", type=int, default=2)
    parser.add_argument(
        "--semantic-dedup", action="store_true",
        help="Drop pair-level near-duplicate answers (same page, ratio>=threshold) after exact-question dedup.",
    )
    parser.add_argument(
        "--semantic-dedup-threshold", type=float, default=ANSWER_NEAR_DUP_THRESHOLD,
        help=f"SequenceMatcher ratio threshold for --semantic-dedup / --semantic-dedup-report (default {ANSWER_NEAR_DUP_THRESHOLD}).",
    )
    parser.add_argument(
        "--semantic-dedup-report", action="store_true",
        help="Write semantic_dedup_report.json listing what --semantic-dedup would drop at the given threshold, without dropping anything (usable with or without --semantic-dedup).",
    )
    args = parser.parse_args()

    try:
        card = run_export(
            args.canonical_path, args.out, schema=args.schema, framework=args.framework,
            min_answer_length=args.min_answer_length, split_by=args.split_by, seed=args.seed,
            intent=args.intent, section_depth=args.section_depth,
            semantic_dedup=args.semantic_dedup, semantic_dedup_threshold=args.semantic_dedup_threshold,
            semantic_dedup_report=args.semantic_dedup_report,
        )
    except ValueError as e:
        print(f"Export refused: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Exported to {args.out}")
    print(json.dumps(card, indent=2, ensure_ascii=False))
