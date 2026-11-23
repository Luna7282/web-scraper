"""code_example@1: code blocks with their surrounding explanation, as
instruction / code pairs. Generic across any site with code in it.

  - wants(): only units containing a fenced code block, so prose units cost
    no call.
  - request(): the unit, plus up to config.CODE_EXAMPLE_CONTEXT_CHARS of the
    preceding unit as context -- the chunker often puts a code block alone
    in a unit and its explanation in the previous one (measured on
    fastapi: 20 of 52 units have fenced code, many of them 27-char units
    holding only the block).
  - Rows: instruction, code (copied verbatim from the unit), language,
    explanation. Whether `code` really is verbatim is checked by the
    schema verifier (Step 5), not trusted here.
"""

from __future__ import annotations

import re

import config
from generators import Generator, PageContext, parse_json_array

_FENCED = re.compile(r"^\s*```", re.MULTILINE)

SYSTEM_PROMPT = """You are an expert training data generator. You are given a passage of technical text that contains one or more fenced code blocks, optionally preceded by context from earlier in the same page.

For each meaningful code block in the TEXT (not in the context), produce one training example:
- "instruction": a self-contained request a user could make that this code fulfils (do not mention "the text" or "the passage").
- "code": the code block's contents copied EXACTLY, character for character, from the TEXT. Never rewrite, shorten, fix or invent code.
- "language": the programming or shell language of the code (e.g. "python", "bash", "json", "text" for terminal output).
- "explanation": one to three sentences, grounded in the text and context, saying what the code does.

Skip code blocks that are empty or meaningless on their own. If there is nothing worth an example, return [].

Return ONLY a JSON array of objects with exactly these keys: "instruction", "code", "language", "explanation"."""


class CodeExampleV1(Generator):
    name, version, record_type = "code_example", 1, "code_example"
    system_prompt = SYSTEM_PROMPT
    schema = {  # noqa: RUF012
        "type": "object", "required": ["instruction", "code", "language", "explanation"],
        "properties": {"instruction": {"type": "string", "minLength": 1}, "code": {"type": "string", "minLength": 1},
                       "language": {"type": "string"}, "explanation": {"type": "string"}},
    }

    def wants(self, unit: str, page: PageContext, index: int) -> bool:
        return bool(_FENCED.search(unit))

    def request(self, unit: str, page: PageContext, index: int) -> tuple[str, str]:
        previous = page.units[index - 1] if 0 < index <= len(page.units) else ""
        context = previous[-config.CODE_EXAMPLE_CONTEXT_CHARS:] if previous else ""
        text = (f"CONTEXT (earlier in the page {page.title or page.url}):\n{context}\n\nTEXT:\n{unit}"
                if context else f"TEXT:\n{unit}")
        return self.system_prompt, text

    def parse(self, raw: str, unit: str, page: PageContext, index: int) -> list[dict]:
        out = []
        for item in parse_json_array(raw):
            if not isinstance(item, dict):
                continue
            instruction, code = item.get("instruction"), item.get("code")
            if not (isinstance(instruction, str) and instruction.strip() and isinstance(code, str) and code.strip()):
                continue
            language = item.get("language")
            explanation = item.get("explanation")
            out.append({"instruction": instruction, "code": code,
                        "language": language if isinstance(language, str) and language.strip() else "unknown",
                        "explanation": explanation if isinstance(explanation, str) else ""})
        return out
