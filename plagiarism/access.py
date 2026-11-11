"""Private identity/session and free-plan admission ledger. No network or text storage."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import unicodedata
from uuid import uuid4


class AccessError(ValueError):
    def __init__(self, message, code="invalid_access_request", status=400, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def canonical_email(email):
    if not isinstance(email, str) or len(email) > 254 or email.count("@") != 1:
        raise AccessError("Укажите корректный адрес электронной почты.")
    email = unicodedata.normalize("NFC", email.strip()).casefold()
    if any(character.isspace() or ord(character) < 32 for character in email):
        raise AccessError("Укажите корректный адрес электронной почты.")
    local, domain = email.rsplit("@", 1)
    try:
        domain = domain.encode("idna").decode("ascii")
        email.encode("utf-8")
    except UnicodeError as error:
        raise AccessError("Укажите корректный адрес электронной почты.") from error
    if not local or len(local) > 64 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain):
        raise AccessError("Укажите корректный адрес электронной почты.")
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
        if not local:
            raise AccessError("Укажите корректный адрес электронной почты.")
    return local + "@" + domain


class AccessLedger:
    CHARACTERS = 6000
    CHECKS = 3
    MODEL_OPERATIONS = 60
    REWRITES = 3
    OPERATION_KINDS = {"judge", "compare", "rewrite", "semantic_retrieval", "lexical_retrieval", "source_index"}

    def __init__(self, path, secret_path=None, now=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise AccessError("Файл доступа не должен быть символической ссылкой.")
        self.secret_path = Path(secret_path) if secret_path else self.path.with_name(self.path.name + ".key")
        self._clock = now or time.time
        self._secret = self._load_secret()
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, email_key TEXT NOT NULL, plan TEXT NOT NULL CHECK(plan='free'),
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                    expires_at REAL NOT NULL, revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS challenges (
                    id_hash TEXT PRIMARY KEY, email_key TEXT NOT NULL, device_key TEXT NOT NULL,
                    expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, consumed_at REAL
                );
                CREATE TABLE IF NOT EXISTS daily_usage (
                    scope TEXT NOT NULL, day TEXT NOT NULL, characters INTEGER NOT NULL DEFAULT 0,
                    checks INTEGER NOT NULL DEFAULT 0, model_ops INTEGER NOT NULL DEFAULT 0,
                    rewrites INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope, day)
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    account_id TEXT NOT NULL REFERENCES accounts(id), request_id TEXT NOT NULL,
                    kind TEXT NOT NULL, report_id TEXT, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                    day TEXT NOT NULL, characters INTEGER NOT NULL, checks INTEGER NOT NULL,
                    model_ops INTEGER NOT NULL, rewrites INTEGER NOT NULL, scopes TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY(account_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS report_access (
                    account_id TEXT NOT NULL REFERENCES accounts(id), report_id TEXT NOT NULL,
                    check_request_id TEXT NOT NULL, PRIMARY KEY(account_id, report_id),
                    UNIQUE(account_id, check_request_id),
                    FOREIGN KEY(account_id, check_request_id) REFERENCES reservations(account_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS operation_report ON reservations(account_id, report_id, kind, state);
                CREATE TABLE IF NOT EXISTS rate_events (
                    bucket TEXT NOT NULL, key_hash TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rate_lookup ON rate_events(bucket, key_hash, created_at);
                CREATE INDEX IF NOT EXISTS rate_expiry ON rate_events(expires_at);
            """)

    def _load_secret(self):
        path = self.secret_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise AccessError("Файл ключа доступа не должен быть символической ссылкой.")
        if not path.exists():
            if self.path.exists() and self.path.stat().st_size:
                raise AccessError("Ключ существующей базы доступа отсутствует. Автоматическая замена запрещена.",
                                  "access_key_missing", 503)
            temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        os.chmod(path, 0o600)
        secret = path.read_bytes()
        if len(secret) != 32:
            raise AccessError("Ключ доступа повреждён. Автоматическая замена отключена.")
        return secret

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _now(self):
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("Clock must include a timezone")
            return value.timestamp()
        return float(value)

    def _period(self, now=None):
        moment = datetime.fromtimestamp(self._now() if now is None else now, timezone.utc)
        day = moment.date()
        reset = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc)
        return day.isoformat(), reset.isoformat()

    def hash_signal(self, value, purpose):
        if not isinstance(value, str) or len(value) > 4096 or not isinstance(purpose, str) or not purpose or len(purpose) > 100:
            raise AccessError("Некорректный сигнал доступа.")
        try:
            encoded = (purpose + "\0" + value).encode("utf-8")
        except UnicodeError as error:
            raise AccessError("Некорректный сигнал доступа.") from error
        return hmac.new(self._secret, encoded, sha256).hexdigest()

    def csrf(self, device, session_token=""):
        if not isinstance(device, str) or not isinstance(session_token, str):
            raise AccessError("Некорректный контекст защищённого запроса.")
        return self.hash_signal(device + "\0" + session_token, "csrf")

    def verify_csrf(self, device, value, session_token=""):
        try:
            return bool(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
                        and hmac.compare_digest(self.csrf(device, session_token), value))
        except AccessError:
            return False

    @staticmethod
    def _token_hash(token):
        if not isinstance(token, str) or not 20 <= len(token) <= 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            return None
        return sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _identifier(value, length=32):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{" + str(length) + r"}", value):
            raise AccessError("Некорректный идентификатор запроса.")

    def ensure_identity(self, issuer, subject, email, email_verified):
        """Accept claims only from the server's trusted, verified identity provider."""
        if email_verified is not True:
            raise AccessError("Сначала подтвердите электронную почту.", "email_not_verified", 403)
        for value in (issuer, subject):
            if not isinstance(value, str) or not value.strip() or len(value) > 512 or "\0" in value:
                raise AccessError("Некорректная подтверждённая личность.")
        account_id = sha256((issuer + "\0" + subject).encode("utf-8")).hexdigest()
        email_key = self.hash_signal(canonical_email(email), "email")
        now = self._now()
        with self.connect() as connection:
            connection.execute("""INSERT INTO accounts VALUES (?, ?, 'free', ?, ?)
                ON CONFLICT(id) DO UPDATE SET email_key=excluded.email_key, updated_at=excluded.updated_at""",
                               (account_id, email_key, now, now))
        return {"id": account_id, "plan": "free", "email_verified": True}

    def _account(self, connection, account_id):
        account = connection.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if account is None:
            raise AccessError("Требуется вход в аккаунт.", "authentication_required", 401)
        return account

    def create_session(self, account_id, ttl_seconds=43200):
        if type(ttl_seconds) not in (int, float) or not 0 < ttl_seconds <= 30 * 86400:
            raise AccessError("Некорректный срок сессии.")
        token = secrets.token_urlsafe(32)
        with self.connect() as connection:
            self._account(connection, account_id)
            connection.execute("INSERT INTO sessions VALUES (?, ?, ?, NULL)",
                               (self._token_hash(token), account_id, self._now() + ttl_seconds))
        return token

    def session(self, token):
        token_hash = self._token_hash(token)
        if not token_hash:
            return None
        with self.connect() as connection:
            row = connection.execute("""SELECT a.id, a.plan FROM sessions s JOIN accounts a ON a.id=s.account_id
                WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?""", (token_hash, self._now())).fetchone()
        return dict(row) | {"email_verified": True} if row else None

    def revoke_session(self, token):
        with self.connect() as connection:
            return connection.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                                      (self._now(), self._token_hash(token))).rowcount > 0

    def create_device(self, ttl_seconds=365 * 86400):
        if type(ttl_seconds) not in (int, float) or not 0 < ttl_seconds <= 365 * 86400:
            raise AccessError("Некорректный срок устройства.")
        token = secrets.token_urlsafe(32)
        with self.connect() as connection:
            connection.execute("INSERT INTO devices VALUES (?, ?)", (self._token_hash(token), self._now() + ttl_seconds))
        return token

    def _device(self, connection, device_id):
        key = self._token_hash(device_id)
        if not key or not connection.execute("SELECT 1 FROM devices WHERE token_hash=? AND expires_at>?",
                                             (key, self._now())).fetchone():
            raise AccessError("Обновите защищённую cookie устройства.", "invalid_device", 403)
        return self.hash_signal(key, "device")

    def valid_device(self, device_id):
        try:
            with self.connect() as connection:
                self._device(connection, device_id)
            return True
        except AccessError:
            return False

    def create_challenge(self, email, device_id, ttl=600):
        if type(ttl) not in (int, float) or not 0 < ttl <= 600:
            raise AccessError("Некорректный срок подтверждения.")
        identifier = uuid4().hex
        with self.connect() as connection:
            device_key = self._device(connection, device_id)
            connection.execute("INSERT INTO challenges VALUES (?, ?, ?, ?, 0, NULL)",
                               (sha256(identifier.encode()).hexdigest(), self.hash_signal(canonical_email(email), "email"),
                                device_key, self._now() + ttl))
        return identifier

    def check_challenge(self, identifier, email, device_id):
        try:
            self._identifier(identifier)
            email_key = self.hash_signal(canonical_email(email), "email")
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                device_key = self._device(connection, device_id)
                key = sha256(identifier.encode()).hexdigest()
                row = connection.execute("SELECT * FROM challenges WHERE id_hash=?", (key,)).fetchone()
                if not row or row["consumed_at"] is not None or row["expires_at"] <= self._now() or row["attempts"] >= 5:
                    return False
                connection.execute("UPDATE challenges SET attempts=attempts+1 WHERE id_hash=?", (key,))
                return hmac.compare_digest(email_key, row["email_key"]) and hmac.compare_digest(device_key, row["device_key"])
        except AccessError:
            return False

    def consume_challenge(self, identifier):
        try:
            self._identifier(identifier)
        except AccessError:
            return False
        with self.connect() as connection:
            return connection.execute("""UPDATE challenges SET consumed_at=? WHERE id_hash=? AND consumed_at IS NULL
                AND expires_at>? AND attempts BETWEEN 1 AND 5""",
                                      (self._now(), sha256(identifier.encode()).hexdigest(), self._now())).rowcount > 0

    def _scopes(self, connection, account_id, device_id=None):
        account = self._account(connection, account_id)
        scopes = ["account:" + account_id, "email:" + account["email_key"]]
        if device_id is not None:
            scopes.append("device:" + self._device(connection, device_id))
        return scopes

    def _usage(self, connection, scopes, day, reset):
        used = {"characters": 0, "checks": 0, "model_ops": 0, "rewrites": 0}
        for scope in scopes:
            row = connection.execute("SELECT * FROM daily_usage WHERE scope=? AND day=?", (scope, day)).fetchone()
            if row:
                for key in used:
                    used[key] = max(used[key], row[key])
        limits = {"characters": self.CHARACTERS, "checks": self.CHECKS,
                  "model_ops": self.MODEL_OPERATIONS, "rewrites": self.REWRITES}
        return {"day": day, "reset_at": reset, "plan": "free", "used": used, "limits": limits,
                "remaining": {key: max(0, limits[key] - used[key]) for key in limits}}

    def usage(self, account_id, device_id=None):
        day, reset = self._period()
        with self.connect() as connection:
            return self._usage(connection, self._scopes(connection, account_id, device_id), day, reset)

    @staticmethod
    def _public_reservation(row, reused=False):
        return {key: row[key] for key in ("account_id", "request_id", "kind", "report_id", "state", "day", "characters")} | {"reused": reused}

    def _reserve(self, connection, account_id, request_id, kind, report_id, fingerprint, scopes, costs):
        self._identifier(request_id)
        self._identifier(fingerprint, 64)
        existing = connection.execute("SELECT * FROM reservations WHERE account_id=? AND request_id=?",
                                      (account_id, request_id)).fetchone()
        if existing:
            if (existing["kind"], existing["report_id"], existing["fingerprint"]) != (kind, report_id, fingerprint):
                raise AccessError("Идентификатор уже использован для другого запроса.", "request_conflict", 409)
            return self._public_reservation(existing, reused=True)
        day, reset = self._period()
        if kind in {"semantic_retrieval", "lexical_retrieval"}:
            previous = connection.execute("""SELECT 1 FROM reservations WHERE account_id=? AND report_id=?
                AND kind=? AND state!='released' LIMIT 1""", (account_id, report_id, kind)).fetchone()
            if previous:
                raise AccessError("Поиск для этого отчёта уже был запущен.", "report_operation_limit", 429)
        used = self._usage(connection, scopes, day, reset)
        if any(used["used"][key] + costs[key] > used["limits"][key] for key in costs):
            if kind == "semantic_retrieval" and costs["model_ops"] > used["remaining"]["model_ops"]:
                raise AccessError("Смысловая сверка этой подборки превышает доступный лимит модели. "
                                  "Доступны обычный поиск совпадений и разбор отдельных фрагментов.",
                                  "daily_quota_exceeded", 429, used)
            raise AccessError("Дневной лимит бесплатного плана исчерпан.", "daily_quota_exceeded", 429, used)
        for scope in scopes:
            connection.execute("""INSERT INTO daily_usage VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope,day) DO UPDATE SET characters=characters+excluded.characters,
                checks=checks+excluded.checks, model_ops=model_ops+excluded.model_ops, rewrites=rewrites+excluded.rewrites""",
                               (scope, day, costs["characters"], costs["checks"], costs["model_ops"], costs["rewrites"]))
        connection.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?, ?)",
                           (account_id, request_id, kind, report_id, fingerprint, day, costs["characters"], costs["checks"],
                            costs["model_ops"], costs["rewrites"], json.dumps(scopes), self._now()))
        return self._public_reservation(connection.execute("SELECT * FROM reservations WHERE account_id=? AND request_id=?",
                                                           (account_id, request_id)).fetchone())

    def reserve_check(self, account_id, device_id, request_id, text, fingerprint):
        if not isinstance(text, str) or not text.strip():
            raise AccessError("Добавьте текст проверки.")
        try:
            text.encode("utf-8")
        except UnicodeError as error:
            raise AccessError("Текст содержит некорректные Unicode-символы.") from error
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scopes = self._scopes(connection, account_id, device_id)
            return self._reserve(connection, account_id, request_id, "check", None, fingerprint, scopes,
                                 {"characters": len(text), "checks": 1, "model_ops": 0, "rewrites": 0})

    def _transition(self, account_id, request_id, target):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._account(connection, account_id)
            row = connection.execute("SELECT * FROM reservations WHERE account_id=? AND request_id=?",
                                     (account_id, request_id)).fetchone()
            if row is None:
                raise AccessError("Запрос не найден.", "reservation_not_found", 404)
            if row["state"] == target:
                return self._public_reservation(row, reused=True)
            allowed = {"started": {"reserved"}, "committed": {"reserved", "started"}, "released": {"reserved"}}
            if row["state"] not in allowed[target]:
                raise AccessError("Запрос уже начат или завершён; автоматический возврат запрещён.", "reservation_state_conflict", 409)
            if target == "released":
                for scope in json.loads(row["scopes"]):
                    connection.execute("""UPDATE daily_usage SET characters=characters-?, checks=checks-?,
                        model_ops=model_ops-?, rewrites=rewrites-? WHERE scope=? AND day=?""",
                                       (row["characters"], row["checks"], row["model_ops"], row["rewrites"], scope, row["day"]))
            connection.execute("UPDATE reservations SET state=? WHERE account_id=? AND request_id=?", (target, account_id, request_id))
            return self._public_reservation(dict(row) | {"state": target})

    def mark_started(self, account_id, request_id):
        return self._transition(account_id, request_id, "started")

    def commit(self, account_id, request_id):
        return self._transition(account_id, request_id, "committed")

    def release(self, account_id, request_id):
        return self._transition(account_id, request_id, "released")

    def bind_report(self, account_id, check_request_id, report_id):
        self._identifier(report_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM reservations WHERE account_id=? AND request_id=? AND kind='check'",
                                     (account_id, check_request_id)).fetchone()
            if not row or row["state"] not in {"started", "committed"}:
                raise AccessError("Сначала начните или сохраните проверку.", "check_required", 403)
            existing = connection.execute("SELECT * FROM report_access WHERE account_id=? AND (report_id=? OR check_request_id=?)",
                                          (account_id, report_id, check_request_id)).fetchone()
            if existing:
                if (existing["report_id"], existing["check_request_id"]) != (report_id, check_request_id):
                    raise AccessError("Проверка уже связана с другим отчётом.", "request_conflict", 409)
            else:
                connection.execute("INSERT INTO report_access VALUES (?, ?, ?)", (account_id, report_id, check_request_id))
        return {"account_id": account_id, "report_id": report_id, "request_id": check_request_id}

    def reserve_operation(self, account_id, report_id, request_id, kind, fingerprint, *, device_id=None,
                          model_ops_cost=1):
        if kind not in self.OPERATION_KINDS:
            raise AccessError("Неизвестная операция.")
        if (type(model_ops_cost) is not int or model_ops_cost < 1
                or kind != "semantic_retrieval" and model_ops_cost != 1):
            raise AccessError("Некорректная стоимость модельной операции.", "invalid_operation_cost", 400)
        self._identifier(report_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._account(connection, account_id)
            parent = connection.execute("""SELECT r.* FROM report_access p JOIN reservations r
                ON r.account_id=p.account_id AND r.request_id=p.check_request_id
                WHERE p.account_id=? AND p.report_id=? AND r.state='committed'""", (account_id, report_id)).fetchone()
            if not parent:
                raise AccessError("Сначала сохраните проверку этого аккаунта.", "check_required", 403)
            # A report may have been created on another device. New work must
            # use the device making this request, rather than its old allowance.
            # _reserve returns an existing reservation unchanged, preserving its
            # original scopes for idempotent retries and an explicit release.
            scopes = (self._scopes(connection, account_id, device_id) if device_id is not None
                      else json.loads(parent["scopes"]))
            return self._reserve(connection, account_id, request_id, kind, report_id, fingerprint, scopes,
                                 {"characters": 0, "checks": 0,
                                  "model_ops": model_ops_cost if kind != "lexical_retrieval" else 0,
                                  "rewrites": int(kind == "rewrite")})

    def hit_rate(self, bucket, key, limit, window):
        if (not isinstance(bucket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", bucket)
                or not isinstance(key, str) or not key or len(key) > 4096
                or type(limit) is not int or not 1 <= limit <= 10000
                or type(window) not in (int, float) or not 0 < window <= 7 * 86400):
            raise AccessError("Некорректные параметры частоты запросов.")
        now = self._now()
        hashed = self.hash_signal(key, "rate:" + bucket)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM rate_events WHERE expires_at<=?", (now,))
            row = connection.execute("SELECT COUNT(*) AS count, MIN(expires_at) AS first FROM rate_events WHERE bucket=? AND key_hash=?",
                                     (bucket, hashed)).fetchone()
            if row["count"] >= limit:
                return max(1, math.ceil(row["first"] - now))
            connection.execute("INSERT INTO rate_events VALUES (?, ?, ?, ?)", (bucket, hashed, now, now + window))
        return 0
