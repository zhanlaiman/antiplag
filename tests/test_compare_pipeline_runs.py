from copy import deepcopy
import unittest

from scripts.compare_pipeline_runs import compare_runs


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


class CompareRunsTests(unittest.TestCase):
    def test_identical_inputs_allow_different_models_and_keep_uncertainty_separate(self):
        first, second = saved_run("qwen3:4b"), saved_run("qwen3.5:9b")
        second["model_digest"] = "f" * 64
        result = compare_runs(first, second)
        self.assertEqual(result["candidate_pairs"], 1)
        self.assertEqual(result["first"]["uncertain_pairs"], 1)
        self.assertEqual(result["first"]["other_pair_errors"], 0)
        self.assertEqual(result["second"]["model"], "qwen3.5:9b")

    def test_changed_candidate_text_is_rejected_even_with_matching_dataset_hash(self):
        first = saved_run("first")
        second = deepcopy(first)
        second["results"][0]["hybrid_qwen"]["pairs"][0]["candidate"]["source_excerpt"] = "Другой источник."
        with self.assertRaisesRegex(ValueError, "фрагменты различаются"):
            compare_runs(first, second)

    def test_changed_code_or_dataset_is_rejected(self):
        first = saved_run("first")
        for field in ("adapter_sha256", "retrieval_sha256", "matching_sha256", "dataset_fingerprint"):
            with self.subTest(field=field):
                second = deepcopy(first)
                second[field] = "0" * 64
                with self.assertRaisesRegex(ValueError, field):
                    compare_runs(first, second)

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
