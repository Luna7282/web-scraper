"""Offline tests for export_formats.py -- pure functions, synthetic
canonical records, no I/O."""
import unittest

from export.export_formats import (
    dedup_by_question,
    mine_triplets,
    semantic_dedup,
    split_records,
    to_alpaca,
    to_conversational,
    to_embedding_pair,
    to_openai_finetune,
    to_prompt_completion,
    to_rag_eval,
    to_raw_text,
    to_vertex,
    validate_records,
)


def _record(**overrides):
    base = {
        "question": "What is X?", "answer": "X is a thing that does Y and Z, in some detail.",
        "source_chunk": "chunk text about X", "source_url": "https://x.com/a", "section": "a",
        "page_title": "A", "generation_model": "deepseek-v4-flash", "extraction_strategy": "per_chunk",
        "timestamp": "2026-08-18T00:00:00+00:00", "crawl_date": "2026-08-18", "license_signal": None,
    }
    base.update(overrides)
    return base


class TestValidateRecords(unittest.TestCase):
    def test_empty_answer_rejected(self):
        valid, rejected = validate_records([_record(answer="")])
        self.assertEqual(valid, [])
        self.assertEqual(rejected[0]["_reject_reason"], "empty_answer")

    def test_question_equals_answer_rejected(self):
        valid, rejected = validate_records([_record(question="same text here", answer="same text here")])
        self.assertEqual(valid, [])
        self.assertEqual(rejected[0]["_reject_reason"], "question_equals_answer")

    def test_short_answer_rejected(self):
        valid, rejected = validate_records([_record(answer="short")], min_answer_length=20)
        self.assertEqual(valid, [])
        self.assertEqual(rejected[0]["_reject_reason"], "answer_too_short")

    def test_good_record_passes(self):
        valid, rejected = validate_records([_record()])
        self.assertEqual(len(valid), 1)
        self.assertEqual(rejected, [])


class TestDedupByQuestion(unittest.TestCase):
    def test_exact_duplicate_removed(self):
        records = [_record(), _record()]
        deduped, removed = dedup_by_question(records)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(removed, 1)

    def test_whitespace_and_case_variants_collide(self):
        records = [_record(question="What Is X?"), _record(question="what   is x?  ")]
        deduped, removed = dedup_by_question(records)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(removed, 1)

    def test_distinct_questions_kept(self):
        records = [_record(question="What is X?"), _record(question="What is Y?")]
        deduped, removed = dedup_by_question(records)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(removed, 0)


class TestSemanticDedup(unittest.TestCase):
    def test_near_duplicate_answers_reduced_to_the_longer_one(self):
        records = [
            _record(source_url="https://x.com/a", question="Is Scene the base class?",
                     answer="Scene is the base class."),
            _record(source_url="https://x.com/a", question="What role does Scene play?",
                     answer="Scene is the base class that every animation inherits from."),
        ]
        deduped, dropped = semantic_dedup(records, threshold=0.5)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["answer"], records[1]["answer"])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["answer"], records[0]["answer"])
        self.assertEqual(dropped[0]["_dedup_reason"], "semantic_near_duplicate")

    def test_distinct_answers_kept(self):
        records = [
            _record(source_url="https://x.com/a", answer="Circle draws a circular mobject."),
            _record(source_url="https://x.com/a", answer="The crawl worker checks robots dot txt before every fetch."),
        ]
        deduped, dropped = semantic_dedup(records, threshold=0.5)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(dropped, [])

    def test_different_pages_not_compared(self):
        records = [
            _record(source_url="https://x.com/a", answer="Scene is the base class."),
            _record(source_url="https://x.com/b", answer="Scene is the base class."),
        ]
        deduped, dropped = semantic_dedup(records, threshold=0.9)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(dropped, [])

    def test_tie_length_keeps_earlier_record(self):
        records = [
            _record(source_url="https://x.com/a", question="Q1", answer="Scene is the animation base class."),
            _record(source_url="https://x.com/a", question="Q2", answer="Scene is the animation root class."),
        ]
        deduped, dropped = semantic_dedup(records, threshold=0.5)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["question"], "Q1")

    def test_dry_run_reports_without_dropping(self):
        records = [
            _record(source_url="https://x.com/a", answer="Scene is the base class."),
            _record(source_url="https://x.com/a", answer="Scene is the base class that everything inherits."),
        ]
        deduped, dropped = semantic_dedup(records, threshold=0.5, dry_run=True)
        self.assertEqual(len(deduped), 2)  # nothing actually removed
        self.assertEqual(len(dropped), 1)

    def test_threshold_configurable_below_threshold_kept(self):
        records = [
            _record(source_url="https://x.com/a", answer="Scene is the base class."),
            _record(source_url="https://x.com/a", answer="Square draws a four-sided mobject entirely unrelated."),
        ]
        deduped, dropped = semantic_dedup(records, threshold=0.9)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(dropped, [])

    def test_redundant_cluster_keeps_only_the_single_longest_answer(self):
        records = [
            _record(source_url="https://x.com/a", question="Q1", answer="Scene is the base class."),
            _record(source_url="https://x.com/a", question="Q2", answer="Scene is the base class too."),
            _record(source_url="https://x.com/a", question="Q3",
                     answer="Scene is the base class that every animation in the library ultimately inherits from."),
        ]
        deduped, dropped = semantic_dedup(records, threshold=0.4)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["question"], "Q3")
        self.assertEqual(len(dropped), 2)


class TestSplitRecords(unittest.TestCase):
    def test_all_rows_from_one_section_land_in_one_split(self):
        # 10 distinct sections -- enough groups for train/val/test to each
        # get at least one whole section under the default 80/10/10 split.
        records = []
        for s in range(10):
            records += [_record(section=f"s{s}", source_url=f"https://x.com/s{s}/{i}") for i in range(3)]
        splits = split_records(records, by="section", seed=1)
        seen_sections: set[str] = set()
        for split_name, rows in splits.items():
            sections_in_split = {r["section"] for r in rows}
            self.assertFalse(sections_in_split & seen_sections, f"{split_name} repeats a section from an earlier split")
            seen_sections |= sections_in_split
            # every row in this split shares its section with no other split
            for other_name, other_rows in splits.items():
                if other_name == split_name:
                    continue
                other_sections = {r["section"] for r in other_rows}
                self.assertFalse(sections_in_split & other_sections)

    def test_two_groups_with_default_ratios_both_land_in_train(self):
        # Documents the small-dataset edge case from the docstring: too few
        # groups for 80/10/10 to produce a non-empty val/test split, rather
        # than silently splitting a group's rows across train and val.
        records = [_record(section="a", source_url=f"https://x.com/a/{i}") for i in range(5)]
        records += [_record(section="b", source_url=f"https://x.com/b/{i}") for i in range(5)]
        splits = split_records(records, by="section", seed=1)
        self.assertEqual(len(splits["train"]), 10)
        self.assertEqual(splits["validation"], [])
        self.assertEqual(splits["test"], [])

    def test_seeded_split_is_reproducible(self):
        records = [_record(section=f"s{i}") for i in range(10)]
        a = split_records(records, seed=7)
        b = split_records(records, seed=7)
        self.assertEqual([r["section"] for r in a["train"]], [r["section"] for r in b["train"]])

    def test_every_record_appears_exactly_once_across_splits(self):
        records = [_record(section=f"s{i}", question=f"Q{i}") for i in range(20)]
        splits = split_records(records, seed=3)
        all_out = splits["train"] + splits["validation"] + splits["test"]
        self.assertEqual(sorted(r["question"] for r in all_out), sorted(r["question"] for r in records))


class TestSchemaProjections(unittest.TestCase):
    def setUp(self):
        self.r = _record()

    def test_conversational(self):
        out = to_conversational(self.r)
        self.assertEqual(out["messages"][0]["role"], "user")
        self.assertEqual(out["messages"][1]["content"], self.r["answer"])

    def test_alpaca(self):
        out = to_alpaca(self.r)
        self.assertEqual(out["instruction"], self.r["question"])
        self.assertEqual(out["input"], "")
        self.assertEqual(out["output"], self.r["answer"])

    def test_prompt_completion(self):
        out = to_prompt_completion(self.r)
        self.assertEqual(out, {"prompt": self.r["question"], "completion": self.r["answer"]})

    def test_embedding_pair_uses_source_chunk_as_positive(self):
        out = to_embedding_pair(self.r)
        self.assertEqual(out["positive"], self.r["source_chunk"])

    def test_rag_eval(self):
        out = to_rag_eval(self.r)
        self.assertEqual(out["context"], self.r["source_chunk"])

    def test_openai_finetune_matches_conversational_shape(self):
        self.assertEqual(to_openai_finetune(self.r), to_conversational(self.r))

    def test_vertex_uses_model_role_not_assistant(self):
        out = to_vertex(self.r)
        self.assertEqual(out["contents"][1]["role"], "model")


class TestRawText(unittest.TestCase):
    def test_dedups_on_normalized_chunk_text(self):
        records = [_record(source_chunk="Some chunk."), _record(source_chunk="some   chunk.")]
        out = to_raw_text(records)
        self.assertEqual(len(out), 1)

    def test_distinct_chunks_kept(self):
        records = [_record(source_chunk="chunk A"), _record(source_chunk="chunk B")]
        out = to_raw_text(records)
        self.assertEqual(len(out), 2)


class TestMineTriplets(unittest.TestCase):
    def test_negative_comes_from_a_different_source_url(self):
        records = [
            _record(source_url="https://x.com/a", source_chunk="chunk from a"),
            _record(source_url="https://x.com/b", source_chunk="chunk from b"),
        ]
        triplets = mine_triplets(records, seed=1)
        for t, r in zip(triplets, records):
            self.assertNotEqual(t["negative"], r["source_chunk"])

    def test_no_triplet_when_only_one_source_url_exists(self):
        records = [_record(source_url="https://x.com/a") for _ in range(3)]
        self.assertEqual(mine_triplets(records), [])


if __name__ == "__main__":
    unittest.main()
