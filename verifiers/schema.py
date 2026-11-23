"""schema@1: a row matches its generator's JSON Schema, plus content rules.

  - Fields: every `required` field present; `type: string` fields are
    strings, `minLength` respected (the generator's own schema, looked up
    from provenance.generator; an unknown generator gives `unknown`).
  - qa rows: the export validation rules (export_formats.validate_records):
    non-empty answer, answer != question, answer at least
    config.VERIFY_MIN_ANSWER_LENGTH characters.
  - code_example rows: `code` must appear in the EXACT text the provider was
    sent for that call (CodeExampleV1.sent_text: the unit plus the stored
    preceding context, capped as the worker capped it), whitespace runs
    collapsed on both sides. Code found nowhere in it was hallucinated or
    rewritten.
"""

from __future__ import annotations

import re

import config
from export.export_formats import validate_records
from verifiers import PageState, Verdict, Verifier

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def check_fields(row: dict, schema: dict) -> list[str]:
    problems = [f"missing field '{k}'" for k in schema.get("required", []) if k not in row]
    for key, prop in schema.get("properties", {}).items():
        if key not in row:
            continue
        value = row[key]
        if prop.get("type") == "string":
            if not isinstance(value, str):
                problems.append(f"field '{key}' is not a string")
            elif len(value.strip()) < prop.get("minLength", 0):
                problems.append(f"field '{key}' is empty")
    return problems


class SchemaV1(Verifier):
    name, version = "schema", 1

    async def verify(self, row: dict, page: PageState) -> Verdict:
        from generators import _registry
        gen = (row.get("provenance") or {}).get("generator") or {}
        cls = _registry().get((gen.get("name"), gen.get("version")))
        if cls is None:
            return Verdict.unknown(f"no schema for generator {gen.get('name')}@{gen.get('version')}")
        if row.get("record_type") != cls.record_type:
            return Verdict.fail(f"record_type {row.get('record_type')!r} is not {cls.record_type!r}")
        problems = check_fields(row, cls.schema)
        if not problems and cls.record_type == "qa":
            _valid, rejected = validate_records([row], min_answer_length=config.VERIFY_MIN_ANSWER_LENGTH)
            problems += [r["_reject_reason"] for r in rejected]
        if not problems and cls.record_type == "code_example" and _norm(row["code"]) not in _norm(cls.sent_text(row)):
            problems.append("code does not appear in the text sent to the model (hallucinated or rewritten)")
        return Verdict.fail(*problems) if problems else Verdict.ok()
