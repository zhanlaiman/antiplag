import json
from pathlib import Path
import tempfile
import unittest

from plagiarism import retrieval_evaluation as evaluation


class RetrievalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "examples.json"
        self.dataset = {
            "id": "synthetic-test", "synthetic": True, "note": "Синтетическая проверка метрик",
            "sources": [
                {"id": "source-a", "group_id": "group-a", "split": "test", "title": "A", "text": "Предисловие. Ожидаемый фрагмент текста. Заключение."},
                {"id": "source-b", "group_id": "group-b", "split": "test", "title": "B", "text": "Отдельный исходный текст для перевода."},
                {"id": "source-train", "group_id": "group-train", "split": "train", "title": "Train", "text": "Обучающий исходный текст."},
            ],
            "cases": [
                {"id": "positive-a", "split": "test", "kind": "deep_paraphrase", "text": "Первый пересказ", "expected_source_id": "source-a", "expected_source_excerpt": "Ожидаемый фрагмент текста."},
                {"id": "positive-b", "split": "test", "kind": "translation", "text": "Второй пересказ", "expected_source_id": "source-b", "expected_source_excerpt": "исходный текст для перевода"},
                {"id": "negative-a", "split": "test", "kind": "independent_same_topic", "text": "Независимое описание", "expected_source_id": None, "expected_source_excerpt": None, "related_source_ids": ["source-a"]},
                {"id": "training", "split": "train", "kind": "exact", "text": "Обучающий исходный текст.", "expected_source_id": "source-train", "expected_source_excerpt": "Обучающий исходный текст."},
            ],
        }
        self.write_dataset()

    def write_dataset(self):
        self.path.write_text(json.dumps(self.dataset, ensure_ascii=False), encoding="utf-8")

    def run_evaluation(self, retrieve_fn, **options):
        return evaluation.run(dataset_path=self.path, retrieve_fn=retrieve_fn, modes=("lexical",), **options)

    def candidate(self, source_id, rank=1, start=0, end=1000):
        return {"source_id": source_id, "rank": rank, "source_start": start, "source_end": end}

    def test_negatives_excluded_from_recall_and_related_candidates_not_false_positives(self):
        def retrieve(text, sources, **options):
            return {"candidates": [self.candidate("source-a")]}

        result = self.run_evaluation(retrieve)
        metrics = result["metrics"]["lexical"]
        self.assertEqual(metrics["positive_cases"], 2)
        self.assertEqual(metrics["source_hit_at_k"], 0.5)
        self.assertEqual(metrics["source_hit_at_1"], 0.5)
        self.assertEqual(metrics["independent_cases_with_related_source"], 1)
        independent = next(item for item in result["results"] if item["example_id"] == "negative-a")
        self.assertIsNone(independent["source_hit"])
        self.assertNotIn("false_positive_rate", metrics)

    def test_correct_document_with_wrong_passage_is_not_span_hit(self):
        def retrieve(text, sources, **options):
            return {"candidates": [self.candidate("source-a", start=0, end=10)]}

        result = self.run_evaluation(retrieve)
        case = result["results"][0]
        self.assertTrue(case["source_hit"])
        self.assertFalse(case["span_hit"])
        self.assertEqual(case["span_coverage"], 0)

    def test_rank_five_hit_and_rank_six_miss(self):
        def retrieve(text, sources, **options):
            rank = 5 if text == "Первый пересказ" else 6
            source_id = "source-a" if text == "Первый пересказ" else "source-b"
            return {"candidates": [self.candidate(source_id, rank=rank)]}

        result = self.run_evaluation(retrieve)
        metrics = result["metrics"]["lexical"]
        self.assertEqual(metrics["source_hit_at_k"], 0.5)
        self.assertEqual(metrics["source_hit_at_1"], 0)
        self.assertEqual(metrics["source_mrr_at_k"], 0.1)

    def test_errors_remain_in_denominator_and_are_visible(self):
        def retrieve(text, sources, **options):
            if text == "Второй пересказ":
                raise RuntimeError("Модель недоступна")
            return {"candidates": [self.candidate("source-a")]}

        result = self.run_evaluation(retrieve)
        metrics = result["metrics"]["lexical"]
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(metrics["source_hit_at_k"], 0.5)
        self.assertEqual(result["results"][1]["error"], "Модель недоступна")

    def test_train_and_test_libraries_remain_disjoint(self):
        libraries = {}

        def retrieve(text, sources, **options):
            libraries[text] = {source["id"] for source in sources}
            return {"candidates": []}

        self.run_evaluation(retrieve, split="all")
        self.assertEqual(libraries["Обучающий исходный текст."], {"source-train"})
        self.assertEqual(libraries["Первый пересказ"], {"source-a", "source-b"})
        self.assertFalse(libraries["Первый пересказ"] & libraries["Обучающий исходный текст."])

    def test_shared_source_group_across_splits_rejected(self):
        self.dataset["sources"][2]["group_id"] = "group-a"
        self.write_dataset()
        with self.assertRaisesRegex(ValueError, "Группа источника"):
            evaluation.load_dataset(self.path)

    def test_invented_or_ambiguous_expected_quote_rejected(self):
        self.dataset["cases"][0]["expected_source_excerpt"] = "Отсутствующая выдержка"
        self.write_dataset()
        with self.assertRaisesRegex(ValueError, "Ожидаемый фрагмент"):
            evaluation.load_dataset(self.path)

    def test_dataset_fingerprint_changes_with_content(self):
        first = evaluation.load_dataset(self.path)["fingerprint"]
        self.dataset["note"] += " Обновлённая версия."
        self.write_dataset()
        second = evaluation.load_dataset(self.path)["fingerprint"]
        self.assertNotEqual(first, second)

    def test_repository_dataset_is_explicitly_synthetic_and_has_distractors(self):
        dataset = evaluation.load_dataset()
        self.assertTrue(dataset["synthetic"])
        self.assertEqual(len(dataset["cases"]), 24)
        for split in ("train", "test"):
            self.assertGreater(len([source for source in dataset["sources"] if source["split"] == split]), 5)


if __name__ == "__main__":
    unittest.main()
