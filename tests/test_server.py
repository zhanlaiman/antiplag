"""Exercise the real HTTP routes with an isolated database and no model server."""

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import server as application
from plagiarism import evaluation, storage
from plagiarism.jobs import Jobs
from plagiarism.storage import Store


SOURCE_TEXT = "Исследователи обнаружили необычные изменения температуры воды в северной части большого озера."


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), application.Handler)
        cls.httpd.daemon_threads = True
        cls.httpd.jobs = Jobs()
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.httpd.jobs.close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.httpd.store = Store(Path(self.temporary.name) / "test.sqlite3")

    def request(self, method, path, payload=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            return response.status, result
        finally:
            connection.close()

    def add_source(self, text=SOURCE_TEXT, title="Источник", url="https://example.org/source"):
        status, body = self.request("POST", "/api/sources", {"title": title, "text": text, "url": url})
        self.assertEqual(status, 200, body)
        return body["source"]

    def check(self, text=SOURCE_TEXT, **options):
        status, body = self.request("POST", "/api/check", {"text": text, **options})
        self.assertEqual(status, 200, body)
        return body["report"]

    def test_report_and_quote_survive_deletion_of_original_source(self):
        source = self.add_source()
        report = self.check()
        match = report["matches"][0]
        self.assertNotIn("sources", report)
        status, body = self.request("DELETE", f"/api/sources/{source['id']}")
        self.assertEqual(status, 200, body)
        status, body = self.request("POST", "/api/rewrite", {
            "report_id": report["id"], "match_id": match["id"], "mode": "quote",
        })
        self.assertEqual(status, 200, body)
        self.assertIn("«" + match["source_excerpt"] + "»", body["rewrite"]["text"])
        self.assertIn(source["url"], body["rewrite"]["text"])
        status, body = self.request("GET", f"/api/reports/{report['id']}")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["report"]["matches"], report["matches"])

    def test_overlapping_matches_from_two_sources_are_counted_once(self):
        self.add_source(title="Первая публикация")
        self.add_source(title="Вторая публикация")
        report = self.check()
        self.assertEqual(report["stats"]["source_count"], 2)
        self.assertEqual(report["stats"]["matched_characters"], len(SOURCE_TEXT.rstrip(".")))
        self.assertLessEqual(report["stats"]["similarity_percent"], 100)

    def test_unknown_selected_source_is_rejected_without_saving_report(self):
        self.add_source()
        status, body = self.request("POST", "/api/check", {"text": SOURCE_TEXT, "source_ids": ["missing"]})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.httpd.store.counts()["reports"], 0)

    def test_invalid_selected_source_type_is_rejected(self):
        self.add_source()
        for source_ids in ("one", [None], [123]):
            with self.subTest(source_ids=source_ids):
                status, body = self.request("POST", "/api/check", {"text": SOURCE_TEXT, "source_ids": source_ids})
                self.assertEqual(status, 400, body)

    def test_invalid_source_url_is_rejected(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "https://", "example.org"):
            with self.subTest(url=url):
                status, body = self.request("POST", "/api/sources", {
                    "title": "Источник", "text": SOURCE_TEXT, "url": url,
                })
                self.assertEqual(status, 400, body)
        self.assertEqual(self.httpd.store.counts()["sources"], 0)

    def test_cross_origin_json_post_cannot_mutate_local_data(self):
        status, body = self.request("POST", "/api/sources", {
            "title": "Источник", "text": SOURCE_TEXT,
        }, headers={"Origin": "https://external.example"})
        self.assertEqual(status, 403, body)
        self.assertEqual(self.httpd.store.counts()["sources"], 0)

    def test_untrusted_host_is_rejected(self):
        status, body = self.request("GET", "/api/sources", headers={"Host": "external.example"})
        self.assertEqual(status, 403, body)

    def test_unicode_offsets_keep_exact_evidence(self):
        self.add_source()
        text = "🧪 Исходные наблюдения. " + SOURCE_TEXT
        report = self.check(text)
        for match in report["matches"]:
            self.assertEqual(text[match["start"]:match["end"]], match["excerpt"])
            self.assertEqual(SOURCE_TEXT[match["source_start"]:match["source_end"]], match["source_excerpt"])

    def test_only_reviewed_training_examples_are_exported(self):
        examples = evaluation.examples()
        training = next(item for item in examples if item["split"] == "train")
        testing = next(item for item in examples if item["split"] == "test")
        for item in (training, testing):
            status, body = self.request("POST", "/api/reviews", {
                "example_id": item["id"], "relation": "same_topic", "comment": "Проверено вручную",
            })
            self.assertEqual(status, 200, body)
        status, body = self.request("POST", "/api/export", {"split": "train"})
        self.assertEqual(status, 200, body)
        self.assertEqual([item["id"] for item in body["records"]], [training["id"]])
        self.assertEqual(body["records"][0]["expected"], "same_topic")
        status, body = self.request("POST", "/api/export", {"split": "test"})
        self.assertEqual(status, 400, body)

    def test_judgment_uses_saved_evidence(self):
        self.add_source()
        report = self.check()
        match = report["matches"][0]
        with patch.object(application.llm, "judge", return_value={"relation": "exact"}) as judge:
            status, body = self.request("POST", "/api/judge", {
                "report_id": report["id"], "match_id": match["id"],
                "text": "Подменённый текст", "source_text": "Подменённый источник",
            })
        self.assertEqual(status, 200, body)
        judge.assert_called_once_with(match["excerpt"], match["source_excerpt"], None)

    def test_check_requires_at_least_one_source(self):
        status, body = self.request("POST", "/api/check", {"text": SOURCE_TEXT})
        self.assertEqual(status, 400, body)

    def test_source_pack_installation_is_idempotent_and_preserves_attribution(self):
        status, body = self.request("GET", "/api/source-packs")
        self.assertEqual(status, 200, body)
        pack = next(item for item in body["packs"] if item["id"] == "economics-ru-v1")
        self.assertEqual(pack["installed_count"], 0)
        for expected_new in (pack["source_count"], 0):
            status, body = self.request("POST", "/api/source-packs/install", {"pack_id": pack["id"]})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["installed_count"], expected_new)
            self.assertEqual(body["sources"][0]["provenance"]["license_name"], "CC BY-SA 4.0")
        status, body = self.request("GET", "/api/source-packs")
        installed = next(item for item in body["packs"] if item["id"] == pack["id"])
        self.assertEqual(installed["installed_count"], pack["source_count"])
        self.assertEqual(self.httpd.store.counts()["sources"], pack["source_count"])

    def test_model_unavailable_does_not_disable_local_matching(self):
        self.add_source()
        unavailable = {"available": False, "model": "qwen3:4b", "models": [], "error": "Ollama недоступна"}
        with patch.object(application.llm, "status", return_value=unavailable):
            status, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["model"]["available"])
        self.assertEqual(body["counts"]["sources"], 1)
        self.assertTrue(self.check()["matches"])

    def test_retrieval_job_uses_report_snapshot_and_saved_candidate_evidence(self):
        source = self.add_source()
        report = self.check()
        candidate = {
            "id": "candidate-1-1", "source_id": source["id"],
            "query_excerpt": report["text"], "source_excerpt": source["text"],
            "source_title": source["title"], "source_url": source["url"],
        }
        result = {"candidates": [candidate], "mode": "lexical", "stats": {}, "warnings": []}
        self.request("DELETE", f"/api/sources/{source['id']}")
        with patch.object(application.retrieval, "retrieve", return_value=result) as retrieve:
            status, body = self.request("POST", "/api/retrieval-jobs", {
                "report_id": report["id"], "mode": "lexical",
            })
            self.assertEqual(status, 200, body)
            job_id = body["job"]["id"]
            for _ in range(100):
                status, body = self.request("GET", f"/api/jobs/{job_id}")
                if body["job"]["status"] in {"complete", "failed"}:
                    break
                time.sleep(0.01)
        self.assertEqual(body["job"]["status"], "complete", body)
        self.assertEqual(retrieve.call_args.args[1][0]["text"], source["text"])
        retrieval_id = body["job"]["result"]["id"]
        status, body = self.request("GET", f"/api/retrievals/{report['id']}")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["retrieval"]["id"], retrieval_id)
        with patch.object(application.llm, "judge", return_value={"relation": "same_topic"}) as judge:
            status, body = self.request("POST", "/api/candidate-judge", {
                "retrieval_id": retrieval_id, "candidate_id": candidate["id"],
                "text": "Подмена", "source_text": "Подмена",
            })
        self.assertEqual(status, 200, body)
        judge.assert_called_once_with(report["text"], source["text"], None)

    def test_retrieval_rejects_invalid_mode_and_boolean_top_k(self):
        self.add_source()
        report = self.check()
        for settings in ({"mode": "other"}, {"top_k": True}, {"top_k": 100}):
            status, body = self.request("POST", "/api/retrieval-jobs", {"report_id": report["id"], **settings})
            self.assertEqual(status, 400, body)

    def test_candidate_quote_replaces_only_grounded_sentence(self):
        prefix = "🧪 Собственная вводная мысль. "
        selected = "В ходе опыта исследователи измеряли температуру воды каждое утро."
        suffix = " Авторский вывод следует сохранить."
        text = prefix + selected + suffix
        source_prefix = "Контекст публикации. "
        candidate = {
            "id": "candidate-1-1", "query_start": 0, "query_end": len(text), "query_excerpt": text,
            "source_excerpt": source_prefix + selected + " Последующее описание.",
            "source_title": "Оригинал", "source_url": "https://example.org/original",
        }
        self.add_source()
        report = self.check(text)
        retrieval = self.httpd.store.save("retrieval", {"report_id": report["id"], "candidates": [candidate]})
        judgment = self.httpd.store.save("candidate-judgment", {
            "retrieval_id": retrieval["id"], "candidate_id": candidate["id"],
            "judgment": {
                "relation": "exact", "evidence_text": selected, "evidence_source": selected,
                "evidence_text_start": len(prefix), "evidence_text_end": len(prefix) + len(selected),
                "evidence_source_start": len(source_prefix), "evidence_source_end": len(source_prefix) + len(selected),
            },
        })
        status, body = self.request("POST", "/api/candidate-rewrite", {
            "retrieval_id": retrieval["id"], "candidate_id": candidate["id"],
            "judgment_id": judgment["id"], "mode": "quote",
        })
        self.assertEqual(status, 200, body)
        rewrite = body["rewrite"]
        self.assertEqual(rewrite["original_text"], selected)
        self.assertEqual(text[rewrite["replacement_start"]:rewrite["replacement_end"]], selected)
        self.assertEqual(text[:rewrite["replacement_start"]], prefix)
        self.assertEqual(text[rewrite["replacement_end"]:], suffix)
        self.assertIn("«" + selected + "»", rewrite["text"])
        self.assertNotIn(source_prefix, rewrite["text"])

    def test_candidate_rewrite_rejects_unrelated_saved_judgment(self):
        self.add_source()
        report = self.check()
        candidate = {"id": "candidate-1-1", "query_excerpt": SOURCE_TEXT, "source_excerpt": SOURCE_TEXT}
        retrieval = self.httpd.store.save("retrieval", {"report_id": report["id"], "candidates": [candidate]})
        wrong = self.httpd.store.save("candidate-judgment", {
            "retrieval_id": "different-retrieval", "candidate_id": candidate["id"], "judgment": {"relation": "exact"},
        })
        status, body = self.request("POST", "/api/candidate-rewrite", {
            "retrieval_id": retrieval["id"], "candidate_id": candidate["id"], "judgment_id": wrong["id"],
        })
        self.assertEqual(status, 400, body)

    def test_later_failed_recheck_invalidates_old_positive_rewrite(self):
        self.add_source()
        report = self.check()
        candidate = {"id": "candidate-1-1", "query_excerpt": SOURCE_TEXT, "source_excerpt": SOURCE_TEXT}
        retrieval = self.httpd.store.save("retrieval", {"report_id": report["id"], "candidates": [candidate]})
        old = self.httpd.store.save("candidate-judgment", {
            "retrieval_id": retrieval["id"], "candidate_id": candidate["id"], "judgment": {"relation": "exact"},
        })
        self.httpd.store.save("candidate-error", {
            "retrieval_id": retrieval["id"], "candidate_id": candidate["id"], "error": "Повторная проверка не завершилась",
        })
        with patch.object(application.llm, "rewrite") as rewrite:
            status, body = self.request("POST", "/api/candidate-rewrite", {
                "retrieval_id": retrieval["id"], "candidate_id": candidate["id"], "judgment_id": old["id"],
            })
        self.assertEqual(status, 400, body)
        rewrite.assert_not_called()

    def test_batch_saves_success_and_failure_before_returning_final_result(self):
        self.add_source()
        report = self.check()
        candidates = [{
            "id": f"candidate-1-{index}", "query_excerpt": SOURCE_TEXT, "source_excerpt": SOURCE_TEXT,
        } for index in (1, 2)]
        retrieval = self.httpd.store.save("retrieval", {"report_id": report["id"], "candidates": candidates})
        with patch.object(application.llm, "judge", side_effect=[
            {"relation": "exact", "model": "qwen3:4b"}, application.llm.OllamaError("Ошибка второй пары"),
        ]):
            status, body = self.request("POST", "/api/candidate-check-jobs", {"retrieval_id": retrieval["id"]})
            self.assertEqual(status, 200, body)
            job_id = body["job"]["id"]
            for _ in range(100):
                status, body = self.request("GET", f"/api/jobs/{job_id}")
                if body["job"]["status"] in {"complete", "failed"}:
                    break
                time.sleep(0.01)
        self.assertEqual(body["job"]["status"], "complete", body)
        self.assertEqual(body["job"]["result"]["counts"], {"total": 2, "positive": 1, "errors": 1, "uncertain": 0})
        # Reopening the store simulates losing the in-memory job registry.
        self.httpd.store = Store(self.httpd.store.path)
        status, body = self.request("GET", f"/api/candidate-judgments/{retrieval['id']}")
        self.assertEqual(status, 200, body)
        results = {item["candidate_id"]: item for item in body["judgments"]}
        self.assertEqual(results[candidates[0]["id"]]["judgment"]["relation"], "exact")
        self.assertEqual(results[candidates[1]["id"]]["error"], "Ошибка второй пары")
        with patch.object(application.llm, "judge", side_effect=application.llm.OllamaError("Повторная ошибка")):
            status, body = self.request("POST", "/api/candidate-judge", {
                "retrieval_id": retrieval["id"], "candidate_id": candidates[0]["id"],
            })
        self.assertEqual(status, 503, body)
        status, body = self.request("GET", f"/api/candidate-judgments/{retrieval['id']}")
        latest = next(item for item in body["judgments"] if item["candidate_id"] == candidates[0]["id"])
        self.assertEqual(latest["error"], "Повторная ошибка")
        self.assertNotIn("judgment", latest)

    def test_match_decisions_persist_and_preserve_original_report(self):
        self.add_source()
        report = self.check()
        match = report["matches"][0]
        original_snapshot = self.httpd.store.get("report", report["id"])
        for decision in ("confirmed", "dismissed", "pending"):
            with self.subTest(decision=decision):
                status, body = self.request("POST", "/api/report-reviews", {
                    "report_id": report["id"], "match_id": match["id"],
                    "decision": decision, "comment": "Решение редактора",
                })
                self.assertEqual(status, 200, body)
                self.httpd.store = Store(self.httpd.store.path)
                status, body = self.request("GET", f"/api/reports/{report['id']}")
                self.assertEqual(status, 200, body)
                saved = body["report"]
                self.assertEqual(saved["reviews"][match["id"]]["decision"], decision)
                self.assertEqual(saved["reviews"][match["id"]]["comment"], "Решение редактора")
                self.assertEqual(saved["stats"], report["stats"])
                self.assertEqual(saved["matches"], report["matches"])
        self.assertEqual(self.httpd.store.get("report", report["id"]), original_snapshot)

    def test_unknown_match_or_decision_cannot_create_review(self):
        self.add_source()
        report = self.check()
        for match_id, decision, expected_status in (
            ("missing", "confirmed", 404),
            (report["matches"][0]["id"], "invalid", 400),
        ):
            with self.subTest(match_id=match_id, decision=decision):
                status, body = self.request("POST", "/api/report-reviews", {
                    "report_id": report["id"], "match_id": match_id, "decision": decision,
                })
                self.assertEqual(status, expected_status, body)
        self.assertEqual(self.httpd.store.match_reviews(report["id"]), {})


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "test.sqlite3")

    def test_source_limit_enforced_inside_storage(self):
        with patch.object(storage, "MAX_SOURCES", 3):
            for number in range(3):
                self.store.add_source(str(number), "Короткий источник", source_id=str(number))
            with self.assertRaises(ValueError):
                self.store.add_source("Лишний источник", "Текст")
            existing = self.store.add_source("Новое название", "Новый текст", source_id="0")
        self.assertEqual(self.store.counts()["sources"], 3)
        self.assertEqual(existing["title"], "0")
        self.assertEqual(existing["text"], "Короткий источник")

    def test_concurrent_inserts_compete_for_one_remaining_slot(self):
        barrier = threading.Barrier(2)

        def attempt(number):
            barrier.wait(timeout=3)
            try:
                self.store.add_source(f"Параллельный {number}", "Текст")
                return "inserted"
            except ValueError:
                return "full"

        with patch.object(storage, "MAX_SOURCES", 3):
            self.store.add_source("Первый", "Текст")
            self.store.add_source("Второй", "Текст")
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, range(2)))
        self.assertCountEqual(results, ["inserted", "full"])
        self.assertEqual(self.store.counts()["sources"], 3)

    def test_demo_failure_does_not_insert_partial_batch(self):
        demos = [{"id": "demo-one", "title": "Первый пример", "text": "Первый текст"},
                 {"id": "demo-two", "title": "Второй пример", "text": "Второй текст"}]
        with patch.object(storage, "MAX_SOURCES", 3):
            self.store.add_source("Первый", "Текст")
            self.store.add_source("Второй", "Текст")
            before = self.store.sources()
            with self.assertRaises(ValueError):
                self.store.add_demo_sources(demos)
        self.assertEqual(self.store.sources(), before)

    def test_demo_retry_at_capacity_preserves_original_rows(self):
        demos = [{"id": "demo-one", "title": "Первый пример", "text": "Первый текст"},
                 {"id": "demo-two", "title": "Второй пример", "text": "Второй текст"}]
        with patch.object(storage, "MAX_SOURCES", 2):
            original = self.store.add_demo_sources(demos)
            changed = [{**item, "text": "Изменённый текст"} for item in demos]
            retried = self.store.add_demo_sources(changed)
        self.assertEqual(retried, original)
        self.assertEqual(self.store.counts()["sources"], 2)

    def test_match_review_survives_reopening_without_changing_snapshot(self):
        source = self.store.add_source("Источник", SOURCE_TEXT)
        report = self.store.save("report", {"text": SOURCE_TEXT, "sources": [source],
                                            "matches": [{"id": "match-1"}], "stats": {"similarity_percent": 98.9}})
        self.store.review_match(report["id"], "match-1", "confirmed", "Проверено")
        self.store.review_match(report["id"], "match-1", "dismissed", "Независимое изложение")
        reopened = Store(self.store.path)
        review = reopened.match_reviews(report["id"])["match-1"]
        self.assertEqual(review["decision"], "dismissed")
        self.assertEqual(review["comment"], "Независимое изложение")
        self.assertEqual(reopened.get("report", report["id"]), report)
        self.assertEqual(len(reopened.match_reviews(report["id"])), 1)


if __name__ == "__main__":
    unittest.main()
