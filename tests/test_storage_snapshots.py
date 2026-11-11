import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from plagiarism.storage import Store, timestamp


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "snapshots.sqlite3")

    def save_report(self, sources, **metadata):
        return self.store.save("report", {
            "text": "Проверяемая статья", "matches": [], "stats": {"similarity_percent": 0},
            "scope": "local_library", "sources": sources, **metadata,
        })

    def snapshot_count(self):
        with self.store.connect() as connection:
            return connection.execute("SELECT count(*) FROM source_snapshots").fetchone()[0]

    def test_repeated_library_is_stored_once_for_one_hundred_sources(self):
        sources = [{"id": str(number), "title": f"Источник {number}",
                    "text": "Длинный исходный материал. " * 100, "url": "", "created_at": "2026-11-11"}
                   for number in range(100)]
        first = self.save_report(sources)
        second = self.save_report(sources)
        self.assertEqual(first["sources"], sources)
        self.assertEqual(second["sources"], sources)
        self.assertEqual(self.snapshot_count(), 100)
        with self.store.connect() as connection:
            rows = connection.execute("SELECT data FROM records WHERE kind = 'report'").fetchall()
            snapshot_size = connection.execute("SELECT sum(length(data)) FROM source_snapshots").fetchone()[0]
        for row in rows:
            stored = json.loads(row[0])
            self.assertNotIn("sources", stored)
            self.assertEqual(len(stored["sources_refs"]), 100)
        self.assertLess(sum(len(row[0]) for row in rows), snapshot_size / 10)
        self.assertEqual(self.store.get("report", first["id"]), first)
        self.assertEqual(self.store.get("report", second["id"]), second)

    def test_snapshot_survives_source_deletion_and_replacement(self):
        original = self.store.add_source("Старое название", "Старый исходный текст", source_id="stable-id")
        first = self.save_report([original])
        self.store.delete_source(original["id"])
        replacement = self.store.add_source("Новое название", "Новый исходный текст", source_id="stable-id")
        second = self.save_report([replacement])
        self.assertEqual(self.snapshot_count(), 2)
        self.assertEqual(self.store.get("report", first["id"])["sources"], [original])
        self.assertEqual(self.store.get("report", second["id"])["sources"], [replacement])

    def test_dictionary_key_order_does_not_duplicate_snapshot(self):
        source = {"id": "one", "title": "Источник", "text": "Исходный текст", "url": ""}
        self.save_report([source])
        self.save_report([dict(reversed(list(source.items())))])
        self.assertEqual(self.snapshot_count(), 1)

    def test_legacy_inline_report_is_read_without_migration(self):
        legacy = {"id": "legacy", "created_at": timestamp(), "text": "Старая статья",
                  "sources": [{"id": "old", "title": "Старый источник", "text": "Сохранённая версия"}],
                  "stats": {"similarity_percent": 25}}
        serialized = json.dumps(legacy, ensure_ascii=False)
        with self.store.connect() as connection:
            connection.execute("INSERT INTO records VALUES (?, ?, ?, ?)",
                               (legacy["id"], "report", serialized, legacy["created_at"]))
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get("report", "legacy"), legacy)
        self.assertEqual(reopened.records("report"), [legacy])
        with reopened.connect() as connection:
            stored = connection.execute("SELECT data FROM records WHERE id = 'legacy'").fetchone()[0]
        self.assertEqual(stored, serialized)
        self.assertEqual(self.snapshot_count(), 0)

    def test_failed_report_insert_rolls_back_new_snapshot(self):
        first = self.save_report([], id="duplicate")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save_report([{"id": "one", "text": "Новая версия"}], id="duplicate")
        self.assertEqual(self.snapshot_count(), 0)
        self.assertEqual(self.store.get("report", "duplicate"), first)

    def test_records_reconstructs_sources_and_preserves_order_and_duplicates(self):
        first_source = {"id": "one", "text": "Первый"}
        second_source = {"id": "two", "text": "Второй"}
        report = self.save_report([second_source, first_source, second_source])
        self.assertEqual(self.snapshot_count(), 2)
        self.assertEqual(self.store.records("report"), [report])
        self.assertNotIn("sources_refs", self.store.get("report", report["id"]))

    def test_missing_snapshot_fails_explicitly(self):
        report = self.save_report([{"id": "one", "text": "Текст"}])
        with self.store.connect() as connection:
            connection.execute("DELETE FROM source_snapshots")
        with self.assertRaisesRegex(RuntimeError, "источник отчёта отсутствует"):
            self.store.get("report", report["id"])

    def test_non_report_records_preserve_existing_format(self):
        data = {"source": {"id": "one", "text": "Источник"}, "sources": ["ordinary-data"]}
        saved = self.store.save("comparison", data)
        self.assertEqual(self.store.get("comparison", saved["id"]), saved)
        self.assertEqual(self.snapshot_count(), 0)


if __name__ == "__main__":
    unittest.main()
