"""Offline end-to-end tests for export.py -- writes a synthetic
canonical.jsonl to a temp dir, runs the real CLI-backing function
(run_export), and checks the actual files it produces. No network."""
import json
import tempfile
import unittest
from pathlib import Path

from export.export import UNSUPPORTED_WITHOUT_EXTRA_PASS, run_export


def _read_jsonl(path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f]


def _canonical_lines(n_sections: int = 6, per_section: int = 4) -> list[dict]:
    records = []
    for s in range(n_sections):
        for i in range(per_section):
            records.append({
                "question": f"What is thing {s}-{i}?",
                "answer": f"Thing {s}-{i} is a detailed answer with enough length to pass validation.",
                "source_chunk": f"chunk text for {s}-{i}",
                "source_url": f"https://x.com/section{s}/page{i}",
                "section": f"section{s}",
                "page_title": f"Page {s}-{i}",
                "generation_model": "deepseek-v4-flash",
                "extraction_strategy": "per_chunk",
                "timestamp": "2026-08-18T00:00:00+00:00",
                "crawl_date": "2026-08-18",
                "license_signal": None,
            })
    return records


class TestRunExport(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.canonical_path = str(Path(self._tmpdir.name) / "canonical.jsonl")
        with open(self.canonical_path, "w", encoding="utf-8") as f:
            for r in _canonical_lines():
                f.write(json.dumps(r) + "\n")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_mlx_framework_produces_three_split_files(self):
        out = str(Path(self._tmpdir.name) / "mlx_out")
        run_export(self.canonical_path, out, schema="alpaca", framework="mlx")
        for name in ("train.jsonl", "valid.jsonl", "test.jsonl"):
            self.assertTrue(Path(out, name).exists())
        train = _read_jsonl(Path(out, "train.jsonl"))
        self.assertTrue(all("instruction" in r for r in train))

    def test_huggingface_framework_produces_dataset_card(self):
        out = str(Path(self._tmpdir.name) / "hf_out")
        run_export(self.canonical_path, out, schema="conversational", framework="huggingface")
        card = json.loads(Path(out, "dataset_card.json").read_text(encoding="utf-8"))
        self.assertIn("row_counts", card)
        self.assertEqual(card["row_counts"]["final"], 24)  # 6 sections x 4 rows, no dupes/rejects

    def test_llama_factory_alpaca_needs_no_column_mapping(self):
        # instruction/input/output already match LLaMA-Factory's default
        # alpaca field names -- verified against the real dataset_info.json,
        # not guessed (export.py's _LLAMA_FACTORY_KNOWN_SCHEMAS comment).
        out = str(Path(self._tmpdir.name) / "lf_out")
        run_export(self.canonical_path, out, schema="alpaca", framework="llama-factory")
        info = json.loads(Path(out, "dataset_info.json").read_text(encoding="utf-8"))
        self.assertIn("canonical_export", info)
        self.assertNotIn("columns", info["canonical_export"])

    def test_llama_factory_conversational_uses_sharegpt_formatting_and_tags(self):
        out = str(Path(self._tmpdir.name) / "lf_sharegpt_out")
        run_export(self.canonical_path, out, schema="conversational", framework="llama-factory")
        info = json.loads(Path(out, "dataset_info.json").read_text(encoding="utf-8"))["canonical_export"]
        self.assertEqual(info["formatting"], "sharegpt")
        self.assertEqual(info["columns"], {"messages": "messages"})
        self.assertEqual(info["tags"]["role_tag"], "role")

    def test_llama_factory_refuses_schema_with_no_known_mapping(self):
        out = str(Path(self._tmpdir.name) / "lf_bad_out")
        with self.assertRaises(ValueError):
            run_export(self.canonical_path, out, schema="rag_eval", framework="llama-factory")

    def test_axolotl_produces_yaml_stanza(self):
        out = str(Path(self._tmpdir.name) / "axo_out")
        run_export(self.canonical_path, out, schema="alpaca", framework="axolotl")
        yaml_text = Path(out, "axolotl_dataset.yaml").read_text(encoding="utf-8")
        self.assertIn("type: alpaca", yaml_text)

    def test_axolotl_conversational_uses_chat_template_type_not_schema_name(self):
        out = str(Path(self._tmpdir.name) / "axo_chat_out")
        run_export(self.canonical_path, out, schema="conversational", framework="axolotl")
        yaml_text = Path(out, "axolotl_dataset.yaml").read_text(encoding="utf-8")
        self.assertIn("type: chat_template", yaml_text)

    def test_axolotl_refuses_schema_with_no_verified_type(self):
        out = str(Path(self._tmpdir.name) / "axo_bad_out")
        with self.assertRaises(ValueError):
            run_export(self.canonical_path, out, schema="prompt_completion", framework="axolotl")

    def test_plain_jsonl_produces_manifest_with_url_prefix_and_row_count(self):
        out = str(Path(self._tmpdir.name) / "plain_out")
        run_export(self.canonical_path, out, schema="prompt_completion", framework="plain-jsonl")
        manifest = json.loads(Path(out, "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 6)  # one entry per section
        for slug, info in manifest.items():
            self.assertIn("url_prefix", info)
            self.assertEqual(info["row_count"], 4)
        unified = _read_jsonl(Path(out, "unified.jsonl"))
        self.assertEqual(len(unified), 24)

    def test_raw_text_batch_projection_writes_split_files_not_qa_pairs(self):
        out = str(Path(self._tmpdir.name) / "raw_out")
        run_export(self.canonical_path, out, schema="raw_text", framework="plain-jsonl")
        train = _read_jsonl(Path(out, "train.jsonl"))
        self.assertTrue(all(set(r.keys()) == {"text"} for r in train))

    def test_triplets_batch_projection(self):
        out = str(Path(self._tmpdir.name) / "triplet_out")
        run_export(self.canonical_path, out, schema="triplets", framework="plain-jsonl")
        train = _read_jsonl(Path(out, "train.jsonl"))
        self.assertTrue(all({"anchor", "positive", "negative"} <= set(r.keys()) for r in train))

    def test_unsupported_schema_refused_with_explanation(self):
        out = str(Path(self._tmpdir.name) / "dpo_out")
        with self.assertRaises(ValueError) as ctx:
            run_export(self.canonical_path, out, schema="dpo", framework="plain-jsonl")
        self.assertIn(UNSUPPORTED_WITHOUT_EXTRA_PASS["dpo"], str(ctx.exception))

    def test_validation_and_dedup_counts_reach_the_dataset_card(self):
        records = _canonical_lines(n_sections=1, per_section=1)
        records.append({**records[0]})  # exact duplicate question
        records.append({**records[0], "answer": ""})  # invalid
        path = str(Path(self._tmpdir.name) / "canonical2.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        out = str(Path(self._tmpdir.name) / "counts_out")
        card = run_export(path, out, schema="alpaca", framework="mlx")
        self.assertEqual(card["row_counts"]["raw_canonical_records"], 3)
        self.assertEqual(card["row_counts"]["rejected_by_validation"], 1)
        self.assertEqual(card["row_counts"]["removed_by_dedup"], 1)
        self.assertEqual(card["row_counts"]["final"], 1)


if __name__ == "__main__":
    unittest.main()
