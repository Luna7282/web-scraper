"""code_example@1: code blocks with their surrounding explanation, as
instruction / code pairs. Generic across any site with code in it.

  - wants(): only units containing a fenced code block, so prose units cost
    no call.
  - request(): the unit, plus up to config.CODE_EXAMPLE_CONTEXT_CHARS of the
    preceding unit as context -- the chunker often puts a code block alone
    in a unit and its explanation in the previous one (measured on
    fastapi: 20 of 52 units have fenced code, many of them 27-char units
    holding only the block).
  - Rows: instruction, code (copied verbatim from what was sent), language,
    explanation, and `context` -- the exact preceding-unit text sent with
    the unit ("" if none). With the row's source_chunk, page_title and
    source_url, sent_text(row) rebuilds the exact request text (capped as
    the worker caps it), so schema@1's "code appears in what was sent"
    check is repeatable from the row alone.
"""

from __future__ import annotations

import re

import config
from generators import USER_PREFIX, Generator, PageContext, parse_json_array

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
        "type": "object", "required": ["instruction", "code", "language", "explanation", "context"],
        "properties": {"instruction": {"type": "string", "minLength": 1}, "code": {"type": "string", "minLength": 1},
                       "language": {"type": "string"}, "explanation": {"type": "string"},
                       "context": {"type": "string"}},
    }

    def wants(self, unit: str, page: PageContext, index: int) -> bool:
        return bool(_FENCED.search(unit))

    @staticmethod
    def context_for(page: PageContext, index: int) -> str:
        previous = page.units[index - 1] if 0 < index <= len(page.units) else ""
        return previous[-config.CODE_EXAMPLE_CONTEXT_CHARS:] if previous else ""

    @staticmethod
    def compose(context: str, unit: str, page_label: str) -> str:
        return f"CONTEXT (earlier in the page {page_label}):\n{context}\n\nTEXT:\n{unit}" if context else f"TEXT:\n{unit}"

    @classmethod
    def sent_text(cls, row: dict) -> str:
        """The exact text the provider received for this row's call (after the
        system prompt): USER_PREFIX + the request text capped at MAX_EXTRACT_CHARS."""
        text = cls.compose(row.get("context") or "", row.get("source_chunk") or "",
                           row.get("page_title") or row.get("source_url") or "")
        return USER_PREFIX + text[:config.MAX_EXTRACT_CHARS]

    def request(self, unit: str, page: PageContext, index: int) -> tuple[str, str]:
        return self.system_prompt, self.compose(self.context_for(page, index), unit, page.title or page.url)

    def parse(self, raw: str, unit: str, page: PageContext, index: int) -> list[dict]:
        context = self.context_for(page, index)
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
                        "explanation": explanation if isinstance(explanation, str) else "", "context": context})
        return out
