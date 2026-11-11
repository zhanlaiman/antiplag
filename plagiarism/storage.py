"""Small, transactional local store. Reports retain their source snapshots."""

import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4
from datetime import datetime, timezone
from hashlib import sha256

from plagiarism.matching import MAX_SOURCES


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = ROOT / "data" / "istok.sqlite3"


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path=DEFAULT_DATABASE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL,
                    url TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS records_kind ON records(kind, created_at);
                CREATE TABLE IF NOT EXISTS source_snapshots (
                    hash TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    example_id TEXT PRIMARY KEY, relation TEXT NOT NULL,
                    comment TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS match_reviews (
                    report_id TEXT NOT NULL, match_id TEXT NOT NULL,
                    decision TEXT NOT NULL, comment TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (report_id, match_id)
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def sources(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM sources ORDER BY created_at DESC, id")]

    def add_source(self, title, text, url="", source_id=None):
        source = dict(id=source_id or uuid4().hex, title=title, text=text, url=url, created_at=timestamp())
        return self._add_sources([source])[0]

    def add_demo_sources(self, sources):
        """Seed the stable demo IDs together, preserving previously stored rows."""
        prepared = [dict(id=item["id"], title=item["title"], text=item["text"],
                         url=item.get("url", ""), created_at=timestamp()) for item in sources]
        return self._add_sources(prepared)

    def _add_sources(self, sources):
        with self.connect() as connection:
            # Serialize the count and insert, including requests from other threads.
            connection.execute("BEGIN IMMEDIATE")
            existing_ids = {row[0] for row in connection.execute("SELECT id FROM sources")}
            new_ids = {source["id"] for source in sources} - existing_ids
            if new_ids and len(existing_ids) + len(new_ids) > MAX_SOURCES:
                raise ValueError(f"В локальной библиотеке доступно до {MAX_SOURCES} источников.")
            connection.executemany(
                "INSERT OR IGNORE INTO sources VALUES (:id, :title, :text, :url, :created_at)", sources
            )
            return [dict(connection.execute("SELECT * FROM sources WHERE id = ?", (source["id"],)).fetchone())
                    for source in sources]

    def delete_source(self, source_id):
        with self.connect() as connection:
            return connection.execute("DELETE FROM sources WHERE id = ?", (source_id,)).rowcount > 0

    def save(self, kind, data):
        record = {**data, "id": data.get("id", uuid4().hex), "created_at": data.get("created_at", timestamp())}
        with self.connect() as connection:
            stored_record = record
            if kind == "report" and "sources" in record:
                references = []
                for source in record["sources"]:
                    serialized = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    snapshot_hash = sha256(serialized.encode("utf-8")).hexdigest()
                    connection.execute("INSERT OR IGNORE INTO source_snapshots VALUES (?, ?)",
                                       (snapshot_hash, serialized))
                    references.append(snapshot_hash)
                stored_record = {key: value for key, value in record.items() if key != "sources"}
                stored_record["sources_refs"] = references
            connection.execute("INSERT INTO records VALUES (?, ?, ?, ?)",
                               (record["id"], kind, json.dumps(stored_record, ensure_ascii=False), record["created_at"]))
        return record

    def get(self, kind, record_id):
        with self.connect() as connection:
            row = connection.execute("SELECT data FROM records WHERE id = ? AND kind = ?", (record_id, kind)).fetchone()
            if row is None:
                return None
            record = json.loads(row[0])
            return self._restore_report_sources(connection, record) if kind == "report" else record

    def records(self, kind, limit=100):
        with self.connect() as connection:
            rows = connection.execute("SELECT data FROM records WHERE kind = ? ORDER BY created_at DESC LIMIT ?", (kind, limit))
            records = [json.loads(row[0]) for row in rows]
            if kind == "report":
                return [self._restore_report_sources(connection, record) for record in records]
            return records

    def _restore_report_sources(self, connection, record):
        # Earlier reports contain their sources inline and remain readable.
        if "sources_refs" not in record or "sources" in record:
            return record
        references = record["sources_refs"]
        sources = []
        for reference in references:
            row = connection.execute("SELECT data FROM source_snapshots WHERE hash = ?", (reference,)).fetchone()
            if row is None:
                raise RuntimeError("Сохранённый источник отчёта отсутствует в базе данных.")
            sources.append(json.loads(row[0]))
        return {key: value for key, value in record.items() if key != "sources_refs"} | {"sources": sources}

    def report_summaries(self):
        # Read only listing fields, leaving potentially large source snapshots on disk.
        with self.connect() as connection:
            rows = connection.execute("""SELECT id, created_at,
                json_extract(data, '$.stats') AS stats,
                substr(json_extract(data, '$.text'), 1, 300) AS text,
                json_extract(data, '$.scope') AS scope
                FROM records WHERE kind = 'report' ORDER BY created_at DESC LIMIT 100""")
            return [{**dict(row), "stats": json.loads(row["stats"])} for row in rows]

    def latest_retrieval(self, report_id):
        with self.connect() as connection:
            row = connection.execute("""SELECT data FROM records
                WHERE kind = 'retrieval' AND json_extract(data, '$.report_id') = ?
                ORDER BY created_at DESC LIMIT 1""", (report_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def candidate_judgments(self, retrieval_id):
        with self.connect() as connection:
            rows = connection.execute("""SELECT data FROM records
                WHERE kind IN ('candidate-judgment', 'candidate-error') AND json_extract(data, '$.retrieval_id') = ?
                ORDER BY created_at DESC""", (retrieval_id,))
            latest = {}
            for row in rows:
                record = json.loads(row[0])
                latest.setdefault(record["candidate_id"], record)
            return list(latest.values())

    def review(self, example_id, relation, comment):
        review = dict(example_id=example_id, relation=relation, comment=comment, updated_at=timestamp())
        with self.connect() as connection:
            connection.execute("""INSERT INTO reviews VALUES (:example_id, :relation, :comment, :updated_at)
                ON CONFLICT(example_id) DO UPDATE SET relation=excluded.relation,
                comment=excluded.comment, updated_at=excluded.updated_at""", review)
        return review

    def reviews(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM reviews ORDER BY updated_at DESC")]

    def review_match(self, report_id, match_id, decision, comment):
        review = dict(report_id=report_id, match_id=match_id, decision=decision,
                      comment=comment, updated_at=timestamp())
        with self.connect() as connection:
            connection.execute("""INSERT INTO match_reviews
                (report_id, match_id, decision, comment, updated_at)
                VALUES (:report_id, :match_id, :decision, :comment, :updated_at)
                ON CONFLICT(report_id, match_id) DO UPDATE SET
                decision=excluded.decision, comment=excluded.comment,
                updated_at=excluded.updated_at""", review)
        return review

    def match_reviews(self, report_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM match_reviews WHERE report_id = ? ORDER BY match_id", (report_id,))
            return {row["match_id"]: dict(row) for row in rows}

    def counts(self):
        with self.connect() as connection:
            return {
                "sources": connection.execute("SELECT count(*) FROM sources").fetchone()[0],
                "reports": connection.execute("SELECT count(*) FROM records WHERE kind = 'report'").fetchone()[0],
            }
