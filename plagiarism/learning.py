"""Private, consented feedback snapshots. No training or external calls.

Only a backend that has checked ownership and extracted the saved pair should
call submit. The queue cannot establish copyright ownership from a URL.
Administrative review and export are offline operations, not account APIs.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import uuid4


RELATIONS = frozenset({"exact", "paraphrase", "same_topic", "unrelated"})
DAILY_LIMIT = 10
PAYLOAD_FIELDS = frozenset({"text", "source_text", "source_url", "source_provenance",
                            "user_label", "user_comment", "model_version"})


class LearningError(ValueError):
    def __init__(self, message, code="invalid_feedback"):
        super().__init__(message)
        self.code = code


def _identifier(value, name, pattern=r"[A-Za-z0-9_-]{1,120}"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise LearningError(f"Некорректный идентификатор {name}.")
    return value


def _label(value):
    if not isinstance(value, str) or value not in RELATIONS:
        raise LearningError("Выберите один из четырёх типов совпадения.")
    return value


def _payload(value):
    if not isinstance(value, dict) or set(value) != PAYLOAD_FIELDS:
        raise LearningError("Для обратной связи нужна только сохранённая пара и её метаданные.")
    for key in ("text", "source_text"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise LearningError("В обратной связи отсутствует исходный фрагмент.")
    if len(value["text"]) + len(value["source_text"]) > 12000:
        raise LearningError("Выберите пару фрагментов суммарно до 12000 символов.")
    _label(value["user_label"])
    if not isinstance(value["user_comment"], str) or len(value["user_comment"]) > 4000:
        raise LearningError("Комментарий должен содержать не более 4000 символов.")
    url = value["source_url"]
    if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 32 for char in url):
        raise LearningError("Некорректная ссылка на источник.")
    if url:
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid:
            raise LearningError("Для источника нужна HTTP-ссылка без данных авторизации.")
    provenance = value["source_provenance"]
    if not isinstance(provenance, dict) or len(provenance) > 32:
        raise LearningError("Некорректные сведения о происхождении источника.")
    for key, item in provenance.items():
        if (not isinstance(key, str) or not 1 <= len(key) <= 80
                or not (item is None or type(item) is bool or type(item) is int and abs(item) <= 10 ** 18
                        or isinstance(item, str) and len(item) <= 4000)):
            raise LearningError("Сведения об источнике должны быть короткими полями метаданных.")
    version = value["model_version"]
    if (not isinstance(version, dict) or not {"model", "judge_version"} <= set(version)
            or set(version) - {"model", "judge_version", "model_digest", "adapter_sha256"}
            or any(not isinstance(version[key], str) or not version[key].strip() or len(version[key]) > 200
                   for key in ("model", "judge_version"))):
        raise LearningError("Укажите модель и версию проверки сохранённой пары.")
    for key in ("model_digest", "adapter_sha256"):
        digest = version.get(key)
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise LearningError("Некорректная контрольная сумма версии модели.")
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise LearningError("Не удалось сохранить данные обратной связи.") from error
    if len(encoded) > 100_000:
        raise LearningError("Обратная связь превышает допустимый размер.")
    return serialized, sha256(encoded).hexdigest()


class LearningQueue:
    def __init__(self, path, now=None):
        self.path = Path(path).absolute()
        self.now = now if now is not None else time.time
        if not callable(self.now):
            raise ValueError("Нужна функция текущего времени.")
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("База обратной связи не может быть символической ссылкой.")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS learning_feedback (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    report_id TEXT NOT NULL, pair_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL, payload TEXT,
                    consent_version TEXT NOT NULL, consented INTEGER NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, created_day TEXT NOT NULL,
                    reviewed_at TEXT, human_label TEXT, rights_confirmed INTEGER NOT NULL DEFAULT 0,
                    review_reason TEXT, revoked_at TEXT,
                    UNIQUE(account_id, report_id, pair_id, payload_sha256)
                );
                CREATE INDEX IF NOT EXISTS learning_daily ON learning_feedback(account_id, created_day);
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _time(self):
        value = self.now()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("Время обратной связи должно содержать часовой пояс.")
            result = value.astimezone(timezone.utc)
        elif type(value) in (int, float) and math.isfinite(value):
            result = datetime.fromtimestamp(value, timezone.utc)
        else:
            raise ValueError("Некорректное время обратной связи.")
        return result.isoformat(), result.date().isoformat()

    @staticmethod
    def _metadata(row):
        return {key: row[key] for key in ("id", "report_id", "pair_id", "payload_sha256", "consent_version",
                                         "state", "created_at", "reviewed_at", "human_label", "revoked_at")} | {
            "consented": row["consented"] == 1, "rights_confirmed": row["rights_confirmed"] == 1,
            "rights_status": "confirmed_by_reviewer" if row["rights_confirmed"] == 1 else "not_verified",
            "payload_available": row["payload"] is not None}

    @staticmethod
    def _snapshot(row):
        raw = row["payload"]
        if raw is None:
            return None
        if sha256(raw.encode("utf-8")).hexdigest() != row["payload_sha256"]:
            raise LearningError("Контрольная сумма обратной связи изменилась. Пример исключён из экспорта.",
                                code="feedback_snapshot_changed")
        value = json.loads(raw)
        _payload(value)
        return value

    def submit(self, account_id, report_id, pair_id, payload, consent_version, consent=True):
        _identifier(account_id, "пользователя", r"[a-f0-9]{64}")
        _identifier(report_id, "отчёта")
        _identifier(pair_id, "пары")
        _identifier(consent_version, "версии согласия", r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
        if consent is not True:
            raise LearningError("Для отправки пары нужно отдельное явное согласие.", code="consent_required")
        serialized, fingerprint = _payload(payload)
        timestamp, day = self._time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("""SELECT * FROM learning_feedback
                WHERE account_id=? AND report_id=? AND pair_id=? AND payload_sha256=?""",
                (account_id, report_id, pair_id, fingerprint)).fetchone()
            if existing:
                return self._metadata(existing)
            used = connection.execute("SELECT COUNT(*) FROM learning_feedback WHERE account_id=? AND created_day=?",
                                      (account_id, day)).fetchone()[0]
            if used >= DAILY_LIMIT:
                raise LearningError("Сегодня уже отправлено десять примеров обратной связи.", code="feedback_daily_limit")
            identifier = uuid4().hex
            connection.execute("""INSERT INTO learning_feedback
                (id,account_id,report_id,pair_id,payload_sha256,payload,consent_version,consented,state,created_at,created_day)
                VALUES (?,?,?,?,?,?,?,1,'pending',?,?)""",
                (identifier, account_id, report_id, pair_id, fingerprint, serialized, consent_version, timestamp, day))
            return self._metadata(connection.execute("SELECT * FROM learning_feedback WHERE id=?", (identifier,)).fetchone())

    def get(self, account_id, identifier):
        _identifier(account_id, "пользователя", r"[a-f0-9]{64}")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM learning_feedback WHERE id=? AND account_id=?",
                                     (identifier, account_id)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return self._metadata(row) | {"payload": self._snapshot(row)}

    def revoke(self, account_id, identifier):
        _identifier(account_id, "пользователя", r"[a-f0-9]{64}")
        timestamp, _ = self._time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM learning_feedback WHERE id=? AND account_id=?",
                                     (identifier, account_id)).fetchone()
            if row is None:
                raise KeyError(identifier)
            connection.execute("""UPDATE learning_feedback SET payload=NULL, consented=0, state='revoked',
                rights_confirmed=0, review_reason=NULL, revoked_at=COALESCE(revoked_at,?) WHERE id=?""", (timestamp, identifier))
            return self._metadata(connection.execute("SELECT * FROM learning_feedback WHERE id=?", (identifier,)).fetchone())

    def review(self, identifier, decision, reason, *, human_label=None, rights_confirmed=False):
        """Offline admin action. User/model labels do not satisfy human review."""
        if decision not in {"approve", "reject"}:
            raise LearningError("Нужны решение approve либо reject.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise LearningError("Для решения нужен комментарий проверяющего до 4000 символов.")
        if decision == "approve":
            _label(human_label)
            if rights_confirmed is not True:
                raise LearningError("Перед одобрением нужно явно подтвердить право использования пары.",
                                    code="feedback_rights_required")
        timestamp, _ = self._time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM learning_feedback WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            if row["state"] == "revoked" or row["consented"] != 1:
                raise LearningError("Согласие на этот пример отозвано.")
            self._snapshot(row)
            connection.execute("""UPDATE learning_feedback SET state=?, reviewed_at=?, human_label=?,
                rights_confirmed=?, review_reason=? WHERE id=?""",
                ("approved" if decision == "approve" else "rejected", timestamp,
                 human_label if decision == "approve" else None, int(decision == "approve"), reason, identifier))
            return self._metadata(connection.execute("SELECT * FROM learning_feedback WHERE id=?", (identifier,)).fetchone())

    def export_approved(self):
        """Return reviewed examples, leaving train/test grouping unassigned.

        rights_confirmed records a reviewer's explicit attestation, not an
        independent legal or license verification by this program.
        """
        with self._connect() as connection:
            rows = connection.execute("""SELECT * FROM learning_feedback WHERE state='approved'
                AND consented=1 AND rights_confirmed=1 AND payload IS NOT NULL ORDER BY created_at,id""").fetchall()
            result = []
            for row in rows:
                if row["human_label"] not in RELATIONS or not row["reviewed_at"]:
                    continue
                payload = self._snapshot(row)
                result.append({**payload, "id": row["id"], "expected": row["human_label"],
                               "snapshot_sha256": row["payload_sha256"], "synthetic": False, "consented": True,
                               "consent_version": row["consent_version"], "human_reviewed": True,
                               "rights_confirmed": True, "rights_verified": False,
                               "rights_status": "confirmed_by_reviewer", "split": "unassigned"})
            return result
