"""code_syntax@1: does a code row's code parse? Parse only -- nothing is
ever executed or compiled to bytecode.

  - python (python, py, python3, pycon, ipython): ast.parse. Doctest-style
    code (">>> " / "... " prompts) is parsed as its prompt lines only, the
    output lines dropped.
  - json: json.loads.
  - Any other language: unknown ("no parser for <language>").
"""

from __future__ import annotations

import ast
import json

from verifiers import PageState, Verdict, Verifier

PYTHON = frozenset({"python", "py", "python3", "pycon", "ipython"})


def doctest_source(code: str) -> str | None:
    lines = code.splitlines()
    if not any(line.lstrip().startswith(">>> ") or line.strip() == ">>>" for line in lines):
        return None
    out = []
    for line in lines:
        s = line.lstrip()
        if s.startswith((">>> ", "... ")):
            out.append(s[4:])
        elif s in (">>>", "..."):
            out.append("")
    return "\n".join(out)


class CodeSyntaxV1(Verifier):
    name, version = "code_syntax", 1
    record_types = ("code_example",)

    async def verify(self, row: dict, page: PageState) -> Verdict:
        language = str(row.get("language") or "").strip().lower()
        code = row.get("code")
        if not isinstance(code, str):
            return Verdict.unknown("no code field")
        if language in PYTHON:
            source = doctest_source(code) or code
            try:
                ast.parse(source)
            except SyntaxError as e:
                return Verdict.fail(f"python syntax error, line {e.lineno}: {e.msg}")
            except ValueError as e:  # e.g. a null byte
                return Verdict.fail(f"python source not parseable: {e}")
            except (RecursionError, MemoryError):
                return Verdict.unknown("python source too deeply nested to parse")
            return Verdict.ok()
        if language == "json":
            try:
                json.loads(code)
            except ValueError as e:
                return Verdict.fail(f"invalid JSON: {e}")
            return Verdict.ok()
        return Verdict.unknown(f"no parser for {language or 'unlabelled code'}")
