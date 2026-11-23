"""qa@1: today's Q&A extraction as a generator -- the same system prompt
(config.EXTRACTION_PROMPT_VARIANT, read when the generator is built), the
same request text, the same parse_qa_json. Rows: question / answer."""

from __future__ import annotations

import config
from content.extraction import (
    CODE_QA_EXTRACTION_SYSTEM_PROMPT,
    QA_EXTRACTION_SYSTEM_PROMPT,
    parse_qa_json,
)
from generators import Generator, PageContext

PROMPTS = {
    config.ExtractionPromptVariant.STANDARD: QA_EXTRACTION_SYSTEM_PROMPT,
    config.ExtractionPromptVariant.CODE_FOCUSED: CODE_QA_EXTRACTION_SYSTEM_PROMPT,
}


class QaV1(Generator):
    name, version, record_type = "qa", 1, "qa"
    generation_meta = True
    quality_scored = True
    schema = {  # noqa: RUF012
        "type": "object", "required": ["question", "answer"],
        "properties": {"question": {"type": "string", "minLength": 1}, "answer": {"type": "string", "minLength": 1}},
    }

    def __init__(self, options: dict | None = None):
        super().__init__(options)
        self.system_prompt = PROMPTS[config.EXTRACTION_PROMPT_VARIANT]

    def parse(self, raw: str, unit: str, page: PageContext, index: int) -> list[dict]:
        # instruction/response kept alongside for quality scoring; the worker
        # writes question/answer only.
        return [{"question": p["instruction"], "answer": p["response"], "_pair": p} for p in parse_qa_json(raw)]
