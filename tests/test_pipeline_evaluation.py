import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from plagiarism import llm, pipeline_evaluation as evaluation


class PipelineEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dataset_path = self.root / "dataset.json"
        self.cache_path = self.root / "checkpoint.json"
        self.sources = [
            {"id": "source-a", "group_id": "group-a", "split": "test", "title": "A", "text": "Первый подробный исходный фрагмент о маршруте трамвая."},
            {"id": "source-b", "group_id": "group-b", "split": "test", "title": "B", "text": "Второй самостоятельный фрагмент о водном наблюдении."},
            {"id": "source-c", "group_id": "group-c", "split": "test", "title": "C", "text": "Третий исходный текст с отдельными фактами."},
        ]
        self.cases = [
            {"id": "positive", "split": "test", "kind": "deep_paraphrase", "text": "Пересказ первого фрагмента с характерными подробностями.", "expected_source_id": "source-a", "expected_source_excerpt": self.sources[0]["text"]},
            {"id": "independent", "split": "test", "kind": "independent_same_topic", "text": "Независимое описание наблюдений за уровнем воды.", "expected_source_id": None, "expected_source_excerpt": None, "related_source_ids": ["source-b"]},
        ]
        self.write_dataset()
        self.judge = Mock(side_effect=self.positive_judgment)

    def write_dataset(self):
        self.dataset_path.write_text(json.dumps({"id": "synthetic-pipeline-test", "synthetic": True,
            "note": "Синтетическая проверка", "sources": self.sources, "cases": self.cases}, ensure_ascii=False), encoding="utf-8")

    def retrieve(self, text, sources, **options):
        selected = [sources[0], sources[1]] if text == self.cases[0]["text"] else [sources[1], sources[2]]
        return {"candidates": [{"id": f"candidate-{rank}", "rank": rank, "source_id": source["id"],
                    "query_start": 0, "query_end": len(text), "query_excerpt": text,
                    "source_start": 0, "source_end": len(source["text"]), "source_excerpt": source["text"]}
                    for rank, source in enumerate(selected, 1)], "stats": {}, "embedding_model": "test-embedding"}

    def baseline(self, text, sources):
        return {"matches": [{"source_id": "source-a"}] if text == self.cases[0]["text"] else []}

    def positive_judgment(self, text, source, model):
        return {"relation": "paraphrase", "explanation": "Тестовая положительная находка",
                "evidence_text": text, "evidence_text_start": 0, "evidence_text_end": len(text),
                "evidence_source": source, "evidence_source_start": 0, "evidence_source_end": len(source)}

    def run_evaluation(self, **options):
        arguments = {"model": "test:model", "digest": "digest-a", "adapter_hash": "adapter-a",
                     "dataset_path": self.dataset_path, "cache_path": self.cache_path,
                     "retrieve_fn": self.retrieve, "judge_fn": self.judge, "analyze_fn": self.baseline}
        arguments.update(options)
        return evaluation.run(**arguments)

    def test_known_independent_and_unverified_pairs_are_distinguished(self):
        result = self.run_evaluation()
        metrics = result["metrics"]["hybrid_qwen"]
        self.assertEqual(metrics["positive_sources_found"], 1)
        self.assertEqual(metrics["independent_articles_with_positive_findings"], 1)
        self.assertEqual(metrics["false_positive_known_independent_source_pairs"], [
            {"example_id": "independent", "source_id": "source-b"}])
        self.assertIn({"example_id": "positive", "source_id": "source-b"}, metrics["unverified_positive_source_pairs"])
        self.assertIn({"example_id": "independent", "source_id": "source-c"}, metrics["unverified_positive_source_pairs"])
        self.assertEqual(metrics["known_independent_source_pairs_evaluated"], 1)

    def test_repeated_run_reuses_all_completed_pairs(self):
        first = self.run_evaluation()
        second = self.run_evaluation()
        self.assertEqual(self.judge.call_count, 4)
        self.assertEqual(first["model_calls"], 4)
        self.assertEqual(second["model_calls"], 0)
        self.assertEqual(second["cache_hits"], 4)
        self.assertEqual(second["metrics"]["hybrid_qwen"]["source_hit_rate"], 1)

    def test_model_digest_and_adapter_changes_invalidate_checkpoint(self):
        self.run_evaluation()
        self.run_evaluation(digest="digest-b")
        self.run_evaluation(adapter_hash="adapter-b")
        self.assertEqual(self.judge.call_count, 12)

    def test_interruption_keeps_completed_pair_and_resume_skips_it(self):
        def interrupt(event, done, total):
            if event["phase"] == "pair":
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_evaluation(on_result=interrupt)
        self.assertEqual(self.judge.call_count, 1)
        result = self.run_evaluation()
        self.assertEqual(result["cache_hits"], 1)
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(self.judge.call_count, 4)

    def test_failed_network_pair_is_reported_and_retried_on_resume(self):
        original = self.positive_judgment
        calls = 0

        def fail_once(text, source, model):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise llm.OllamaError("Временная ошибка связи")
            return original(text, source, model)

        self.judge.side_effect = fail_once
        first = self.run_evaluation()
        second = self.run_evaluation()
        self.assertEqual(first["metrics"]["hybrid_qwen"]["errors"], 1)
        self.assertEqual(first["metrics"]["hybrid_qwen"]["missed_expected_sources"], ["positive"])
        self.assertEqual(second["model_calls"], 1)
        self.assertEqual(second["cache_hits"], 3)
        self.assertEqual(second["metrics"]["hybrid_qwen"]["errors"], 0)

    def test_ungrounded_positive_is_rejected_and_not_cached(self):
        def invalid(text, source, model):
            answer = self.positive_judgment(text, source, model)
            answer["evidence_source"] = "Выдуманная выдержка"
            return answer

        self.judge.side_effect = invalid
        result = self.run_evaluation()
        self.assertEqual(result["metrics"]["hybrid_qwen"]["errors"], 4)
        self.assertEqual(result["metrics"]["hybrid_qwen"]["positive_sources_found"], 0)
        self.assertFalse(self.cache_path.exists())

    def test_literal_offsets_are_mapped_back_to_original_unicode_text(self):
        prefix = "🧪 Введение. "
        original = self.cases[0]["text"]
        self.cases[0]["text"] = prefix + original
        self.write_dataset()

        def retrieve(text, sources, **options):
            source = sources[0]
            return {"candidates": [{"id": "one", "rank": 1, "source_id": source["id"],
                "query_start": len(prefix), "query_end": len(text), "query_excerpt": original,
                "source_start": 0, "source_end": len(source["text"]), "source_excerpt": source["text"]}]}

        result = self.run_evaluation(retrieve_fn=retrieve, max_cases=1)
        evidence = result["results"][0]["hybrid_qwen"]["pairs"][0]["evidence"]
        self.assertEqual(evidence["evidence_text_start"], len(prefix))
        self.assertEqual(self.cases[0]["text"][evidence["evidence_text_start"]:evidence["evidence_text_end"]], original)

    def test_retrieval_failure_is_not_reported_as_completed_clean_article(self):
        def fail(*arguments, **options):
            raise llm.OllamaError("Поиск недоступен")

        result = self.run_evaluation(retrieve_fn=fail)
        metrics = result["metrics"]["hybrid_qwen"]
        self.assertEqual(metrics["errors"], 2)
        self.assertEqual(metrics["independent_articles_fully_processed"], 0)
        self.assertEqual(result["model_calls"], 0)

    def test_top_k_and_max_cases_are_applied_without_modifying_dataset(self):
        original_bytes = self.dataset_path.read_bytes()
        result = self.run_evaluation(max_cases=1, top_k=1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(self.dataset_path.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
