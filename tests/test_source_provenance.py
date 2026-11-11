import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from plagiarism.storage import Store


class SourceProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "store.sqlite3"

    def test_existing_library_migrates_without_changing_old_text(self):
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL, url TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.execute("INSERT INTO sources VALUES (?, ?, ?, ?, ?)", ("old", "Старый источник", "Сохранённый текст", "", "2026-01-01"))
        connection.close()
        store = Store(self.path)
        source = store.sources()[0]
        self.assertEqual(source["text"], "Сохранённый текст")
        self.assertEqual(source["provenance"], {})
        self.assertEqual(Store(self.path).sources(), [source])

    def test_collection_preserves_attribution_and_returns_atomic_counts(self):
        store = Store(self.path)
        provenance = {"license_name": "CC BY-SA 4.0", "attribution": "Авторы публикации", "revision_id": 123}
        sources = [{"id": "collection-source", "title": "Статья", "text": "Исходный текст статьи", "url": "https://example.org/article", "provenance": provenance}]
        first = store.install_sources(sources)
        second = store.install_sources(sources)
        self.assertEqual((first["installed_count"], first["already_present_count"]), (1, 0))
        self.assertEqual((second["installed_count"], second["already_present_count"]), (0, 1))
        self.assertEqual(first["sources"], second["sources"])
        self.assertEqual(store.sources()[0]["provenance"], provenance)
        report = store.save("report", {"text": "Проверяемый текст", "sources": first["sources"]})
        store.delete_source("collection-source")
        self.assertEqual(store.get("report", report["id"])["sources"][0]["provenance"], provenance)

    def test_latest_candidate_attempt_follows_commit_order_when_clock_moves_backwards(self):
        store = Store(self.path)
        store.save("candidate-judgment", {
            "retrieval_id": "retrieval", "candidate_id": "candidate", "created_at": "2026-11-11T10:00:00+00:00",
            "judgment": {"relation": "exact"},
        })
        latest = store.save("candidate-error", {
            "retrieval_id": "retrieval", "candidate_id": "candidate", "created_at": "2026-11-11T09:59:00+00:00",
            "error": "Новую оценку не удалось подтвердить",
        })
        self.assertEqual(store.candidate_judgments("retrieval")[0]["id"], latest["id"])


if __name__ == "__main__":
    unittest.main()
