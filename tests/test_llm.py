import json
import io
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from plagiarism import evaluation, llm
from scripts import evaluate as evaluate_cli


def chat_response(result):
    return {"message": {"content": json.dumps(result, ensure_ascii=False)}, "done": True}


class JudgmentTests(unittest.TestCase):
    def test_rejects_invented_evidence(self):
        response = chat_response({
            "relation": "paraphrase", "explanation": "Похожие детали.",
            "evidence_text_id": "T99",
            "evidence_source_id": "S1",
        })
        with patch.object(llm, "_request", return_value=response):
            with self.assertRaisesRegex(llm.OllamaError, "отсутствует"):
                llm.judge("Проверяемый текст о библиотеке.", "Источник сообщает о городской библиотеке.")

    def test_accepts_valid_evidence_and_sends_schema(self):
        passage = "На мосту установили четыре синих отметки уровня воды."
        response = chat_response({
            "relation": "exact", "explanation": "Предложение совпадает дословно.",
            "evidence_text_id": "T1", "evidence_source_id": "S1",
        })
        with patch.object(llm, "_request", return_value=response) as request:
            result = llm.judge(passage, passage, model="qwen3:4b")
        self.assertEqual(result["relation"], "exact")
        self.assertTrue(result["requires_review"])
        self.assertEqual(result["evidence_text"], passage)
        self.assertEqual(result["evidence_source"], passage)
        self.assertEqual(result["evidence_text_start"], 0)
        self.assertEqual(result["evidence_text_end"], len(passage))
        self.assertEqual(result["evidence_method"], "sentence_selection")
        payload = request.call_args.args[1]
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["format"]["properties"]["evidence_text_id"]["enum"], ["", "T1"])

    def test_negative_relation_can_have_no_evidence(self):
        response = chat_response({
            "relation": "same_topic", "explanation": "Совпадает только тема.",
            "evidence_text_id": "", "evidence_source_id": "",
        })
        with patch.object(llm, "_request", return_value=response):
            result = llm.judge("Новый текст о снеге.", "Другой текст о снеге.")
        self.assertEqual(result["relation"], "same_topic")
        self.assertIsNone(result["evidence_text_start"])
        self.assertIsNone(result["evidence_source_end"])

    def test_missing_or_invalid_fields_rejected(self):
        invalid = [{}, [], {"relation": "sure", "explanation": "Да.",
                              "evidence_text_id": "", "evidence_source_id": ""}]
        for value in invalid:
            with self.subTest(value=value):
                with patch.object(llm, "_request", return_value=chat_response(value)):
                    with self.assertRaises(llm.OllamaError):
                        llm.judge("Проверяемый текст.", "Текст источника.")

    def test_selects_original_span_without_normalizing_or_transcribing(self):
        text = "Вступление.  Волонтёры записали звонок — в депо.\nЗаключение."
        source = "Волонтёры записали звонок — в депо."
        response = chat_response({
            "relation": "exact", "explanation": "Второе предложение совпадает.",
            "evidence_text_id": "T2", "evidence_source_id": "S1",
        })
        with patch.object(llm, "_request", return_value=response):
            result = llm.judge(text, source)
        self.assertEqual(result["evidence_text"], source)
        self.assertIn(result["evidence_text"], text)
        self.assertEqual(text[result["evidence_text_start"]:result["evidence_text_end"]], source)

    def test_evidence_offsets_select_correct_occurrence_of_repeated_sentence(self):
        sentence = "Один и тот же подробный фрагмент."
        text = f"\n🙂 Вступление.\n  {sentence}\n  {sentence}  "
        source = f"  {sentence}  "
        response = chat_response({
            "relation": "exact", "explanation": "Выбрано последнее предложение.",
            "evidence_text_id": "T3", "evidence_source_id": "S1",
        })
        with patch.object(llm, "_request", return_value=response):
            result = llm.judge(text, source)
        self.assertEqual(result["evidence_text_start"], text.rindex(sentence))
        self.assertEqual(result["evidence_source_start"], 2)
        self.assertEqual(text[result["evidence_text_start"]:result["evidence_text_end"]], sentence)
        self.assertEqual(source[result["evidence_source_start"]:result["evidence_source_end"]], sentence)

    def test_positive_requires_nonempty_evidence(self):
        response = chat_response({
            "relation": "paraphrase", "explanation": "Похожие детали.",
            "evidence_text_id": "", "evidence_source_id": "S1",
        })
        with patch.object(llm, "_request", return_value=response):
            with self.assertRaisesRegex(llm.OllamaError, "выдержки"):
                llm.judge("Содержательный фрагмент.", "Содержательный источник.")

    def test_many_lines_are_grouped_into_bounded_literal_spans(self):
        text = "\n".join(f"Строка {number}." for number in range(200))
        segments = llm._segments(text, "T")
        self.assertLessEqual(len(segments), 48)
        for segment in segments.values():
            self.assertIn(segment["text"], text)
            self.assertEqual(text[segment["start"]:segment["end"]], segment["text"])

    def test_quote_uses_verbatim_source_without_model(self):
        source = "Сотрудники проверяют всхожесть семян раз в два года."
        with patch.object(llm, "_request") as request:
            result = llm.rewrite("Неточный пересказ.", source, "Архив семян", "", mode="quote")
        request.assert_not_called()
        self.assertIn(f"«{source}»", result["text"])
        self.assertIn("Архив семян", result["text"])

    def test_paraphrase_keeps_citation_program_controlled(self):
        with patch.object(llm, "_request", return_value=chat_response({"text": "Новая формулировка."})):
            result = llm.rewrite("Исходная формулировка.", "Текст источника.",
                                 "Название источника", "https://example.org/article")
        self.assertTrue(result["text"].endswith("Источник: Название источника — https://example.org/article"))
        self.assertTrue(result["requires_review"])

    def test_long_input_rejected_before_network(self):
        with patch.object(llm, "_request") as request:
            with self.assertRaises(ValueError):
                llm.judge("а" * (llm.MAX_PASSAGE_LENGTH + 1), "Короткий источник.")
        request.assert_not_called()

    def test_status_handles_server_unavailable(self):
        with patch.object(llm, "urlopen", side_effect=URLError("connection refused")):
            status = llm.status()
        self.assertFalse(status["available"])
        self.assertEqual(status["models"], [])
        self.assertIn("Ollama", status["error"])

    def test_missing_embedding_model_error_names_requested_model(self):
        response_body = io.BytesIO(b"Missing")
        error = HTTPError("http://localhost/api/embed", 404, "Missing", {}, response_body)
        with patch.object(llm, "urlopen", side_effect=error):
            with self.assertRaisesRegex(llm.OllamaError, "bge-m3"):
                llm.embed(["Непустой фрагмент."], model="bge-m3")
        self.assertTrue(response_body.closed)

    def test_generation_and_embedding_queue_on_shared_model_lock(self):
        generation_started, release_generation, embedding_finished = Event(), Event(), Event()
        request_paths = []
        passage = "Достаточно подробный фрагмент для сравнения."

        def request(path, payload=None, timeout=180):
            request_paths.append(path)
            if path == "/api/chat":
                generation_started.set()
                if not release_generation.wait(timeout=3):
                    raise AssertionError("Test generation was never released")
                return chat_response({
                    "relation": "exact", "explanation": "Фрагменты совпадают.",
                    "evidence_text_id": "T1", "evidence_source_id": "S1",
                })
            return {"embeddings": [[1.0, 0.0]]}

        def embedding():
            try:
                return llm.embed([passage])
            finally:
                embedding_finished.set()

        with patch.object(llm, "_request", side_effect=request):
            with ThreadPoolExecutor(max_workers=2) as executor:
                generation = executor.submit(llm.judge, passage, passage)
                try:
                    self.assertTrue(generation_started.wait(timeout=1))
                    vectors = executor.submit(embedding)
                    # The former one-second timeout failed a legitimate queued
                    # retrieval while generation was still using the model.
                    self.assertFalse(embedding_finished.wait(timeout=1.1))
                    self.assertEqual(request_paths, ["/api/chat"])
                finally:
                    release_generation.set()
                self.assertEqual(generation.result(timeout=2)["relation"], "exact")
                self.assertEqual(vectors.result(timeout=2), [[1.0, 0.0]])
        self.assertEqual(request_paths, ["/api/chat", "/api/embed"])

    def test_status_separates_embedding_and_completion_models(self):
        with patch.object(llm, "_request", return_value={"models": [
            {"name": "qwen3:4b"}, {"name": "bge-m3:latest"}
        ]}):
            status = llm.status()
        self.assertEqual(status["models"], ["qwen3:4b"])
        self.assertEqual(status["embedding_models"], ["bge-m3:latest"])
        self.assertTrue(status["embedding_available"])

    def test_embedding_payload_and_vectors(self):
        with patch.object(llm, "_request", return_value={"embeddings": [[0.1, 0.2], [0.3, 0.4]]}) as request:
            vectors = llm.embed(["Первая фраза.", "Вторая фраза."])
        self.assertEqual(len(vectors), 2)
        self.assertEqual(request.call_args.args[0], "/api/embed")
        self.assertFalse(request.call_args.args[1]["truncate"])

    def test_embedding_rejects_wrong_counts_dimensions_and_nonfinite_values(self):
        invalid = [[[1.0]], [[1.0], [1.0, 2.0]], [[1.0], [float("nan")]], [[True], [False]]]
        for vectors in invalid:
            with self.subTest(vectors=vectors):
                with patch.object(llm, "_request", return_value={"embeddings": vectors}):
                    with self.assertRaises(llm.OllamaError):
                        llm.embed(["Первая фраза.", "Вторая фраза."])


class EvaluationTests(unittest.TestCase):
    def test_source_groups_do_not_leak_between_splits(self):
        cases = evaluation.examples()
        train_sources = {item["source_id"] for item in cases if item["split"] == "train"}
        test_sources = {item["source_id"] for item in cases if item["split"] == "test"}
        self.assertTrue(train_sources.isdisjoint(test_sources))
        self.assertEqual(len({item["id"] for item in cases}), len(cases))

    def test_false_positive_is_measured_and_errors_visible(self):
        cases = [
            {"id": "a", "source_id": "s1", "expected": "exact", "split": "test", "text": "A", "source_text": "A"},
            {"id": "b", "source_id": "s2", "expected": "same_topic", "split": "test", "text": "B", "source_text": "B"},
            {"id": "c", "source_id": "s3", "expected": "unrelated", "split": "test", "text": "C", "source_text": "C"},
        ]
        with patch.object(evaluation, "examples", return_value=cases):
            with patch.object(llm, "judge", side_effect=[
                {"relation": "exact"}, {"relation": "paraphrase"}, llm.OllamaError("Нет ответа")
            ]):
                result = evaluation.run()
        metrics = result["metrics"]
        self.assertEqual(metrics["tp"], 1)
        self.assertEqual(metrics["fp"], 1)
        self.assertEqual(metrics["precision"], 0.5)
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(metrics["accuracy"], 0.3333)
        self.assertIsNone(result["results"][2]["predicted"])
        self.assertIn("error", result["results"][2])

    def test_request_error_counts_as_missed_positive_in_recall_all_cases(self):
        cases = [
            {"id": "a", "source_id": "s1", "expected": "exact", "split": "test", "text": "A", "source_text": "A"},
            {"id": "b", "source_id": "s2", "expected": "paraphrase", "split": "test", "text": "B", "source_text": "B"},
        ]
        with patch.object(evaluation, "examples", return_value=cases):
            with patch.object(llm, "judge", side_effect=[{"relation": "exact"}, llm.OllamaError("Нет ответа")]):
                result = evaluation.run()
        self.assertEqual(result["metrics"]["recall"], 1.0)
        self.assertEqual(result["metrics"]["recall_all_cases"], 0.5)
        self.assertEqual(result["metrics"]["coverage"], 0.5)

    def test_fingerprint_changes_when_actual_text_changes(self):
        original = [{"id": "a", "source_id": "s1", "expected": "exact", "split": "test", "text": "A", "source_text": "A"}]
        changed = [original[0] | {"text": "B"}]
        self.assertNotEqual(evaluation.dataset_fingerprint(original), evaluation.dataset_fingerprint(changed))
        self.assertEqual(evaluation.dataset_fingerprint(original), evaluation.dataset_fingerprint(original))

    def test_incompatible_baseline_is_rejected_before_model_work(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.json"
            baseline.write_text(json.dumps({"results": [], "dataset_fingerprint": "wrong"}), encoding="utf-8")
            with patch("sys.argv", ["evaluate.py", "--baseline", str(baseline)]):
                with patch.object(llm, "status") as status:
                    with redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as raised:
                            evaluate_cli.main()
            self.assertEqual(raised.exception.code, 1)
            status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
