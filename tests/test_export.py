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

    def test_plain_jsonl_disambiguates_a_real_slug_collision_not_just_the_unit(self):
        """step 8 Phase 2A: the real Part D corpus (35 sections at depth
        2/3) hit the length-cap+hash-suffix path (5 slugs) but never
        triggered an actual collision -- disambiguate_slugs()'s own unit
        tests cover the logic in isolation, but that's exactly the kind
        of isolation gap LESSONS_LEARNED.md #28 (chrome_strip) warns
        about. This constructs three section labels that genuinely
        collide after slugification ("a!b", "a?b", "a.b" all -> "a-b")
        and proves the disambiguation survives a real run through
        run_export -> package_plain_jsonl, not just the pure function."""
        def _row(url, question, section):
            return {
                "question": question,
                "answer": f"Answer for {question}, long enough to pass validation checks.",
                "source_chunk": "chunk", "source_url": url, "section": section,
                "page_title": "T", "generation_model": "m", "extraction_strategy": "per_chunk",
                "timestamp": "t", "crawl_date": "d", "license_signal": None,
            }

        records = [
            _row("https://x.com/a!b", "Q1", "a!b"),
            _row("https://x.com/a?b", "Q2", "a?b"),
            _row("https://x.com/a.b", "Q3", "a.b"),
        ]
        path = str(Path(self._tmpdir.name) / "collision_canonical.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        out = str(Path(self._tmpdir.name) / "collision_out")
        run_export(path, out, schema="alpaca", framework="plain-jsonl", split_by="source_url")

        section_files = sorted(p.name for p in Path(out, "sections").iterdir())
        self.assertEqual(section_files, ["a-b-1.jsonl", "a-b-2.jsonl", "a-b.jsonl"])
        manifest = json.loads(Path(out, "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 3)  # not silently merged into one
        self.assertEqual(sum(v["row_count"] for v in manifest.values()), 3)

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

    def _write_near_duplicate_pair(self) -> str:
        records = [
            {
                "question": "Is Scene the base class?", "answer": "Scene is the base class.",
                "source_chunk": "chunk a", "source_url": "https://x.com/a/page", "section": "a",
                "page_title": "Page", "generation_model": "deepseek-v4-flash", "extraction_strategy": "per_chunk",
                "timestamp": "2026-08-18T00:00:00+00:00", "crawl_date": "2026-08-18", "license_signal": None,
            },
            {
                "question": "What role does Scene play?",
                "answer": "Scene is the base class that every animation inherits from.",
                "source_chunk": "chunk a", "source_url": "https://x.com/a/page", "section": "a",
                "page_title": "Page", "generation_model": "deepseek-v4-flash", "extraction_strategy": "per_chunk",
                "timestamp": "2026-08-18T00:00:00+00:00", "crawl_date": "2026-08-18", "license_signal": None,
            },
        ]
        path = str(Path(self._tmpdir.name) / "near_dup.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return path

    def test_semantic_dedup_report_writes_candidates_without_dropping(self):
        path = self._write_near_duplicate_pair()
        out = str(Path(self._tmpdir.name) / "report_out")
        card = run_export(
            path, out, schema="alpaca", framework="mlx",
            semantic_dedup_report=True, semantic_dedup_threshold=0.5,
        )
        self.assertEqual(card["row_counts"]["final"], 2)  # report-only: nothing actually dropped
        report = json.loads(Path(out, "semantic_dedup_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["applied"], False)
        self.assertEqual(report["candidate_count"], 1)

    def test_semantic_dedup_applied_removes_the_near_duplicate(self):
        path = self._write_near_duplicate_pair()
        out = str(Path(self._tmpdir.name) / "applied_out")
        card = run_export(
            path, out, schema="alpaca", framework="mlx",
            semantic_dedup=True, semantic_dedup_threshold=0.5,
        )
        self.assertEqual(card["row_counts"]["removed_by_semantic_dedup"], 1)
        self.assertEqual(card["row_counts"]["final"], 1)
        train = _read_jsonl(Path(out, "train.jsonl"))
        self.assertEqual(len(train), 1)

    def test_semantic_dedup_off_by_default(self):
        path = self._write_near_duplicate_pair()
        out = str(Path(self._tmpdir.name) / "default_out")
        card = run_export(path, out, schema="alpaca", framework="mlx")
        self.assertEqual(card["row_counts"]["removed_by_semantic_dedup"], 0)
        self.assertEqual(card["row_counts"]["final"], 2)
        self.assertFalse(Path(out, "semantic_dedup_report.json").exists())


if __name__ == "__main__":
    unittest.main()
