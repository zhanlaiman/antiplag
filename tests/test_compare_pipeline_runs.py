from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.compare_pipeline_runs import compare_runs, main


def saved_run(model):
    candidate = {"id": "candidate-1", "query_index": 0, "rank": 1,
                 "query_start": 0, "query_end": 15, "query_excerpt": "Исходный текст.",
                 "source_id": "source-1", "source_start": 0, "source_end": 17,
                 "source_excerpt": "Текст источника."}
    return {"model": model, "model_digest": "a" * 64, "dataset_fingerprint": "b" * 64,
            "adapter_sha256": "c" * 64, "retrieval_sha256": "d" * 64, "matching_sha256": "e" * 64,
            "split": "test", "top_k": 5, "model_calls": 2, "elapsed_seconds": 10.0,
            "metrics": {"hybrid_qwen": {"errors": 1}},
            "results": [{"example_id": "case-1", "kind": "exact", "expected_source_id": "source-1",
                         "hybrid_qwen": {"pairs": [{"candidate": candidate, "elapsed_seconds": 10.0,
                                                     "error": "Не подтверждено", "error_code": "unconfirmed_exact"}]}}]}


def run_with_two_pairs():
    run = saved_run("first")
    pairs = run["results"][0]["hybrid_qwen"]["pairs"]
    second = deepcopy(pairs[0])
    second["candidate"].update(id="candidate-2", rank=2, source_id="source-2",
                               source_excerpt="Другой фрагмент.")
    pairs.append(second)
    return run


class CompareRunsTests(unittest.TestCase):
    def test_identical_inputs_allow_different_models_and_keep_uncertainty_separate(self):
        first, second = saved_run("qwen3:4b"), saved_run("qwen3.5:9b")
        second["model_digest"] = "f" * 64
        result = compare_runs(first, second)
        self.assertEqual(result["candidate_pairs"], 1)
        self.assertEqual(result["first"]["uncertain_pairs"], 1)
        self.assertEqual(result["first"]["other_pair_errors"], 0)
        self.assertEqual(result["second"]["model"], "qwen3.5:9b")
        self.assertEqual(result["comparison_mode"], "strict")
        self.assertEqual(result["candidate_order_changed_cases"], [])

    def test_reordered_identical_pairs_require_explicit_alignment(self):
        first = run_with_two_pairs()
        second = deepcopy(first)
        second["results"][0]["hybrid_qwen"]["pairs"].reverse()
        with self.assertRaisesRegex(ValueError, "порядок кандидатов"):
            compare_runs(first, second)

        for rank, pair in enumerate(second["results"][0]["hybrid_qwen"]["pairs"], 1):
            pair["candidate"].update(id=f"new-id-{rank}", rank=rank)
        result = compare_runs(first, second, align_identical_inputs=True)
        self.assertEqual(result["comparison_mode"], "align_identical_inputs")
        self.assertEqual(result["candidate_pairs"], 2)
        self.assertEqual(result["candidate_order_changed_cases"], ["case-1"])
        self.assertIn("оборудования", result["latency_note"])
        self.assertIn("порядка", result["latency_note"])

    def test_alignment_rejects_each_changed_exact_input_field(self):
        first = run_with_two_pairs()
        changes = {"query_index": 1, "query_start": 1, "query_end": 14,
                   "query_excerpt": "Другой текст.", "source_id": "source-changed",
                   "source_start": 1, "source_end": 16, "source_excerpt": "Другой источник."}
        for field, value in changes.items():
            with self.subTest(field=field):
                second = deepcopy(first)
                second["results"][0]["hybrid_qwen"]["pairs"][0]["candidate"][field] = value
                with self.assertRaisesRegex(ValueError, "Точные входы"):
                    compare_runs(first, second, align_identical_inputs=True)

    def test_alignment_counts_duplicates_and_rejects_added_or_missing_inputs(self):
        first = run_with_two_pairs()
        pairs = first["results"][0]["hybrid_qwen"]["pairs"]
        pairs.append(deepcopy(pairs[1]))
        reordered = deepcopy(first)
        reordered["results"][0]["hybrid_qwen"]["pairs"].reverse()
        self.assertEqual(compare_runs(first, reordered, align_identical_inputs=True)["candidate_pairs"], 3)

        for change in ("remove", "add", "replace_duplicate"):
            with self.subTest(change=change):
                second = deepcopy(first)
                second_pairs = second["results"][0]["hybrid_qwen"]["pairs"]
                if change == "remove":
                    second_pairs.pop()
                elif change == "add":
                    second_pairs.append(deepcopy(second_pairs[0]))
                else:
                    second_pairs[2] = deepcopy(second_pairs[0])
                with self.assertRaisesRegex(ValueError, "число повторений"):
                    compare_runs(first, second, align_identical_inputs=True)

    def test_alignment_keeps_cases_and_all_gold_metadata_fixed(self):
        first = run_with_two_pairs()
        first["results"][0].update(split="test", expected_source_excerpt="Эталонный фрагмент.",
                                    library_source_count=8)
        changes = {"example_id": "another-case", "kind": "translation", "expected_source_id": None,
                   "split": "train", "expected_source_excerpt": "Другая разметка.",
                   "library_source_count": 9, "new_gold_field": "changed"}
        for field, value in changes.items():
            with self.subTest(field=field):
                second = deepcopy(first)
                second["results"][0][field] = value
                with self.assertRaisesRegex(ValueError, "метаданные"):
                    compare_runs(first, second, align_identical_inputs=True)
        for cases in ([], first["results"] * 2):
            second = deepcopy(first)
            second["results"] = cases
            with self.assertRaisesRegex(ValueError, "Состав исходных статей"):
                compare_runs(first, second, align_identical_inputs=True)

    def test_cli_alignment_flag_is_explicit_and_reported(self):
        first = run_with_two_pairs()
        second = deepcopy(first)
        second["results"][0]["hybrid_qwen"]["pairs"].reverse()
        with TemporaryDirectory() as directory:
            first_path, second_path = Path(directory) / "first.json", Path(directory) / "second.json"
            first_path.write_text(json.dumps(first), encoding="utf-8")
            second_path.write_text(json.dumps(second), encoding="utf-8")
            output = io.StringIO()
            with patch("sys.argv", ["compare_pipeline_runs", str(first_path), str(second_path),
                                    "--align-identical-inputs"]), redirect_stdout(output):
                main()
            result = json.loads(output.getvalue())
            self.assertEqual(result["comparison_mode"], "align_identical_inputs")

    def test_changed_candidate_text_is_rejected_even_with_matching_dataset_hash(self):
        first = saved_run("first")
        second = deepcopy(first)
        second["results"][0]["hybrid_qwen"]["pairs"][0]["candidate"]["source_excerpt"] = "Другой источник."
        with self.assertRaisesRegex(ValueError, "фрагменты различаются"):
            compare_runs(first, second)

    def test_changed_code_or_dataset_is_rejected(self):
        first = saved_run("first")
        for aligned in (False, True):
            for field in ("adapter_sha256", "retrieval_sha256", "matching_sha256", "dataset_fingerprint",
                          "split", "top_k"):
                with self.subTest(field=field, aligned=aligned):
                    second = deepcopy(first)
                    second[field] = "0" * 64
                    with self.assertRaisesRegex(ValueError, field):
                        compare_runs(first, second, align_identical_inputs=aligned)

    def test_missing_model_digest_is_rejected(self):
        first = saved_run("first")
        del first["model_digest"]
        with self.assertRaisesRegex(ValueError, "model_digest"):
            compare_runs(first, saved_run("second"))

    def test_cached_answers_are_excluded_from_inference_latency(self):
        first = saved_run("first")
        pair = first["results"][0]["hybrid_qwen"]["pairs"][0]
        pair["cached"] = True
        result = compare_runs(first, deepcopy(first))
        self.assertEqual(result["first"]["pair_latency"]["cached_pairs"], 1)
        self.assertIsNone(result["first"]["pair_latency"]["median_seconds"])

    def test_old_error_without_code_stays_unclassified(self):
        first = saved_run("first")
        del first["results"][0]["hybrid_qwen"]["pairs"][0]["error_code"]
        result = compare_runs(first, deepcopy(first))
        self.assertEqual(result["first"]["uncertain_pairs"], 0)
        self.assertEqual(result["first"]["other_pair_errors"], 0)
        self.assertEqual(result["first"]["unclassified_pair_errors"], 1)


if __name__ == "__main__":
    unittest.main()
