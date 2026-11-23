"""Pluggable generators (headless contract v2, Brief 3B Step 4, item 12).

A generator turns one extraction unit (plus its page context) into rows of
one `record_type`. It is single-call by design: the generator says WHAT to
send (`request`) and how to read the answer (`parse`); the worker makes the
call. That keeps every call behind the existing breaker, unit semaphore,
429 / 5xx handling, cost ledger and timing log -- a generator can't make an
uncounted or ungated call, and a malformed answer is accounted exactly as
today's Q&A extraction accounts it.

Selected in a v2 config by name and version:
    "generators": [{"name": "qa", "version": 1}, {"name": "code_example", "version": 1}]
(default: qa@1 only). Several generators run on the same units; their calls
add up per unit that each one `wants`.

Registered generators: REGISTRY. A new generator: subclass Generator, give
it a name / version / record_type / schema (JSON Schema of the fields it
returns), and register it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from content.extraction import MalformedExtractionError

USER_PREFIX = "Text to process:\n\n"   # what the worker's LLM call puts before the request text
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass(frozen=True)
class PageContext:
    url: str
    title: str | None
    section: str | None
    units: tuple[str, ...] = field(default=(), repr=False)   # every unit of the page, in chunk order


class Generator:
    name: str = ""
    version: int = 0
    record_type: str = ""
    schema: dict = {}            # JSON Schema of the fields parse() returns  # noqa: RUF012
    system_prompt: str = ""
    # qa rows also carry generation_model / extraction_strategy (v1 row layout)
    generation_meta: bool = False
    # quality scoring (System One sample quality) reads instruction/response: qa only
    quality_scored: bool = False

    def __init__(self, options: dict | None = None):
        if options:
            raise ValueError(f"generator {self.id} takes no options, got {sorted(options)}")
        self.options = {}

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()

    def spec(self) -> dict:
        return {"name": self.name, "version": self.version}

    def wants(self, unit: str, page: PageContext, index: int) -> bool:
        """Cheap, no call: whether this unit is worth a call."""
        return True

    def request(self, unit: str, page: PageContext, index: int) -> tuple[str, str]:
        """(system prompt, text). The worker sends USER_PREFIX + text, capped."""
        return self.system_prompt, unit

    def parse(self, raw: str, unit: str, page: PageContext, index: int) -> list[dict]:
        """The response -> a list of field dicts (this record type's own
        fields). [] is a valid answer; MalformedExtractionError if unreadable."""
        raise NotImplementedError


def parse_json_array(text: str) -> list:
    """Raw text, then fence-stripped, then first '[' .. last ']'
    (parse_qa_json's salvage order). MalformedExtractionError if none parses."""
    candidates = [text.strip()]
    fenced = _FENCE.sub("", text).strip()
    if fenced != candidates[0]:
        candidates.append(fenced)
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed
    raise MalformedExtractionError(f"could not parse a JSON array from response: {text[:200]!r}")


def _registry() -> dict[tuple[str, int], type[Generator]]:
    from generators.code_example import CodeExampleV1
    from generators.qa import QaV1
    return {(g.name, g.version): g for g in (QaV1, CodeExampleV1)}


REGISTRY_IDS = ("qa@1", "code_example@1")
DEFAULT_SPECS = ({"name": "qa", "version": 1},)


def validate_specs(specs) -> None:
    """Shape and registry check for a config's `generators` (ValueError)."""
    if not isinstance(specs, list) or not specs:
        raise ValueError("generators must be a non-empty list of {name, version, options?}")
    seen = set()
    reg = _registry()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) - {"name", "version", "options"} or "name" not in spec \
                or "version" not in spec:
            raise ValueError("each generator must be {name, version, options?}")
        key = (spec["name"], spec["version"])
        if type(spec["version"]) is not int or key not in reg:
            raise ValueError(f"unknown generator {spec['name']!r} version {spec['version']!r}; "
                             f"available: {', '.join(REGISTRY_IDS)}")
        if key in seen:
            raise ValueError(f"generator {spec['name']}@{spec['version']} listed twice")
        seen.add(key)
        if not isinstance(spec.get("options", {}), dict):
            msg = f"generator {spec['name']}@{spec['version']} options must be an object"
            raise ValueError(msg)  # noqa: TRY004 -- a config error, reported as such
        reg[key](spec.get("options"))  # options check


def build(specs=None) -> list[Generator]:
    reg = _registry()
    return [reg[(s["name"], s["version"])](s.get("options")) for s in (specs or DEFAULT_SPECS)]
