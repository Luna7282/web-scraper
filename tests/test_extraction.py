"""Tests for extraction.py's salvage-vs-fail JSON parsing. Both shapes
explicitly required by the task: prose-wrapped (salvageable, the common
case) and truly malformed (must fail loudly, not silently return [])."""
import unittest

from content.extraction import parse_qa_json, MalformedExtractionError


class TestParseQaJson(unittest.TestCase):
    def test_clean_json_array(self):
        text = '[{"instruction": "Q1", "response": "A1"}]'
        self.assertEqual(parse_qa_json(text), [{"instruction": "Q1", "response": "A1"}])

    def test_markdown_fenced_json(self):
        text = '```json\n[{"instruction": "Q1", "response": "A1"}]\n```'
        self.assertEqual(parse_qa_json(text), [{"instruction": "Q1", "response": "A1"}])

    def test_prose_wrapped_json_salvaged(self):
        text = (
            "Here is the extracted content:\n\n"
            '[{"instruction": "Q1", "response": "A1"}]\n\n'
            "Let me know if you need anything else!"
        )
        self.assertEqual(parse_qa_json(text), [{"instruction": "Q1", "response": "A1"}])

    def test_empty_array_is_valid_not_malformed(self):
        # The LLM legitimately found nothing extractable -- must NOT raise.
        self.assertEqual(parse_qa_json("[]"), [])

    def test_empty_array_wrapped_in_prose_is_valid(self):
        text = "I reviewed the page and found no extractable technical content.\n\n[]"
        self.assertEqual(parse_qa_json(text), [])

    def test_truly_malformed_raises(self):
        text = "I'm sorry, I cannot process this request in JSON format."
        with self.assertRaises(MalformedExtractionError):
            parse_qa_json(text)

    def test_truncated_json_raises(self):
        text = '[{"instruction": "Q1", "respo'
        with self.assertRaises(MalformedExtractionError):
            parse_qa_json(text)

    def test_non_dict_array_entries_filtered_not_malformed(self):
        text = '["just", "strings", "not", "objects"]'
        self.assertEqual(parse_qa_json(text), [])

    def test_entries_missing_required_keys_filtered(self):
        text = '[{"instruction": "Q1", "response": "A1"}, {"instruction": "Q2"}]'
        self.assertEqual(parse_qa_json(text), [{"instruction": "Q1", "response": "A1"}])


if __name__ == "__main__":
    unittest.main()
