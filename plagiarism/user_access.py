"""Local password credentials bound to unchanged guest spaces, using AccessLedger."""

from contextlib import closing, contextmanager
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import unicodedata

from plagiarism.access import AccessError, AccessLedger
from plagiarism.password_auth import password_digest


BASE_COLUMNS = {
    "accounts": {"id", "email_key", "plan", "created_at", "updated_at"},
    "sessions": {"token_hash", "account_id", "expires_at", "revoked_at"},
    "devices": {"token_hash", "expires_at"},
    "challenges": {"id_hash", "email_key", "device_key", "expires_at", "attempts", "consumed_at"},
    "daily_usage": {"scope", "day", "characters", "checks", "model_ops", "rewrites"},
    "reservations": {"account_id", "request_id", "kind", "report_id", "fingerprint", "state", "day", "characters", "checks", "model_ops", "rewrites", "scopes", "created_at"},
    "report_access": {"account_id", "report_id", "check_request_id"},
    "rate_events": {"bucket", "key_hash", "created_at", "expires_at"},
    "local_credentials": {"account_id", "login_key", "salt", "digest"},
    "account_guest_bindings": {"account_id", "guest_id", "bound_at"},
}
WEAK_PASSWORDS = frozenset({"passwordpassword", "123456789012345", "qwertyuiopasdfgh", "парольпарольпароль"})


def unavailable():
    return AccessError("Проверка аккаунта временно недоступна. Повторите позже.", "user_access_unavailable", 503)


def login_name(value):
    if not isinstance(value, str) or len(value) > 64:
        raise AccessError("Логин: 3–32 символа латиницы, цифры, точка, дефис или подчёркивание.", "invalid_username", 400)
    value = unicodedata.normalize("NFKC", value).casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}", value):
        raise AccessError("Логин: 3–32 символа латиницы, цифры, точка, дефис или подчёркивание.", "invalid_username", 400)
    return value


def _password(value, *, registering=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise AccessError("Укажите пароль длиной до 256 символов.", "invalid_password", 400)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise AccessError("Некорректный пароль.", "invalid_password", 400) from None
    if registering and (len(value) < 15 or value.casefold() in WEAK_PASSWORDS):
        raise AccessError("Выберите уникальный пароль от 15 символов.", "weak_password", 400)
    return value


class _BoundLedger(AccessLedger):
    """Keep existing primitives, but never recreate a lost initialized database."""
    def __init__(self, *args, **kwargs):
        self.ready = False
        super().__init__(*args, **kwargs)
        self.ready = True

    @contextmanager
    def connect(self):
        try:
            if not self.ready:
                with super().connect() as db:
                    yield db
                return
            if self.path.is_symlink() or self.secret_path.is_symlink() or not self.secret_path.is_file():
                raise OSError("access state missing")
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            raise unavailable() from None

    def valid_device(self, device_id):
        with self.connect() as db:
            try:
                self._device(db, device_id)
                return True
            except AccessError as error:
                if error.code == "invalid_device":
                    return False
                raise


def _triples(db, minimum=0, *, exact=False):
    counts = db.execute("SELECT (SELECT count(*) FROM accounts),(SELECT count(*) FROM local_credentials),"
        "(SELECT count(*) FROM account_guest_bindings),"
        "(SELECT count(*) FROM accounts a JOIN local_credentials c ON c.account_id=a.id "
        "JOIN account_guest_bindings b ON b.account_id=a.id WHERE a.email_key='' AND a.plan='free')").fetchone()
    if (len(set(counts)) != 1 or counts[0] < minimum or exact and counts[0] != minimum
            or db.execute("PRAGMA foreign_key_check").fetchone()):
        raise unavailable()
    invalid = db.execute("SELECT 1 FROM accounts a JOIN local_credentials c ON c.account_id=a.id "
        "JOIN account_guest_bindings b ON b.account_id=a.id WHERE "
        "length(a.id)!=64 OR a.id GLOB '*[^0-9a-f]*' OR length(b.guest_id)!=64 OR b.guest_id GLOB '*[^0-9a-f]*' "
        "OR length(c.login_key)!=64 OR c.login_key GLOB '*[^0-9a-f]*' OR length(c.salt)!=64 OR c.salt GLOB '*[^0-9a-f]*' "
        "OR length(c.digest)!=64 OR c.digest GLOB '*[^0-9a-f]*' LIMIT 1").fetchone()
    if invalid:
        raise unavailable()


def _validate_existing(path):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            for table, columns in BASE_COLUMNS.items():
                kind = db.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
                info = list(db.execute("SELECT name,pk FROM pragma_table_info(?)", (table,)))
                if not kind or kind[0] != "table" or not columns <= {row["name"] for row in info}:
                    raise unavailable()
                if table in {"accounts", "sessions", "devices", "local_credentials", "account_guest_bindings"}:
                    primary = "id" if table == "accounts" else "token_hash" if table in {"sessions", "devices"} else "account_id"
                    if not any(row["name"] == primary and row["pk"] == 1 for row in info):
                        raise unavailable()
            for table, unique in (("local_credentials", "login_key"), ("account_guest_bindings", "guest_id")):
                keys = []
                for index in db.execute('SELECT name FROM pragma_index_list(?) WHERE "unique"=1 AND partial=0', (table,)):
                    keys.append([row[0] for row in db.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[0],))])
                foreign = {(row["from"], row["table"], row["to"]) for row in db.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,))}
                if [unique] not in keys or ("account_id", "accounts", "id") not in foreign:
                    raise unavailable()
            for table, required in {
                    "sessions": {("account_id", "accounts", "id")},
                    "reservations": {("account_id", "accounts", "id")},
                    "report_access": {("account_id", "accounts", "id"), ("account_id", "reservations", "account_id"), ("check_request_id", "reservations", "request_id")},
            }.items():
                foreign = {(row["from"], row["table"], row["to"]) for row in db.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,))}
                if not required <= foreign:
                    raise unavailable()
            _triples(db)
    except (OSError, sqlite3.Error):
        raise unavailable() from None


class UserAccess:
    def __init__(self, root, *, guard, claim, persist=lambda: None, now=time.time):
        self.guard, self.claim, self._persist, self.now = guard, claim, persist, now
        # Initialization and every successful claim share the same durable beta lock.
        # An old reader constructor cannot publish a lower marker after a writer claim.
        with self.claim(None):
            self._initialize(root, now)
        self.persist()

    def _initialize(self, root, now):
        root = Path(root); private = root / "private"; runtime = root / "runtime"
        if private.is_symlink() or runtime.is_symlink():
            raise unavailable()
        private.mkdir(mode=0o700, exist_ok=True); runtime.mkdir(mode=0o700, exist_ok=True)
        self.path = private / "user-access.sqlite3"
        self.key_path = self.path.with_name(self.path.name + ".key")
        marker = self.marker = runtime / "user-access-initialized"
        existed = self.path.exists()
        if (self.path.is_symlink() or marker.is_symlink() or self.key_path.is_symlink()
                or marker.exists() and (not existed or not self.key_path.is_file())):
            raise unavailable()
        if existed:
            if not self.key_path.is_file():
                raise unavailable()
            _validate_existing(self.path)
        self.ledger = _BoundLedger(self.path, now=now)
        if not existed:
            with self.ledger.connect() as db:
                db.executescript("""
                    CREATE TABLE local_credentials(
                        account_id TEXT PRIMARY KEY REFERENCES accounts(id), login_key TEXT NOT NULL UNIQUE,
                        salt TEXT NOT NULL, digest TEXT NOT NULL);
                    CREATE TABLE account_guest_bindings(
                        account_id TEXT PRIMARY KEY REFERENCES accounts(id), guest_id TEXT NOT NULL UNIQUE,
                        bound_at REAL NOT NULL);
                """)
        _validate_existing(self.path)
        with self.ledger.connect() as db:
            count = db.execute("SELECT count(*) FROM accounts").fetchone()[0]
            if marker.exists():
                _triples(db, self._floor())
        self._record_count(count)

    def _floor(self):
        try:
            if self.marker.is_symlink() or self.marker.stat().st_size > 64:
                raise OSError("invalid marker")
            value = self.marker.read_text()
            match = re.fullmatch(r"user-access-v1:([0-9]{1,12})\n", value)
            if not match:
                raise OSError("invalid marker")
            return int(match[1])
        except (OSError, UnicodeError):
            raise unavailable() from None

    def _integrity(self, db):
        # A commit whose marker could not be published cannot issue any later session.
        # Only serialized initialization may roll that valid pending state forward.
        _triples(db, self._floor(), exact=True)

    def _record_count(self, count):
        # Registration holds the beta claim lock: concurrent successful claims cannot lower this floor.
        if self.marker.exists() and count < self._floor():
            raise unavailable()
        temporary = self.marker.with_name(self.marker.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as stream:
                stream.write(f"user-access-v1:{count}\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.marker)
            descriptor = os.open(self.marker.parent, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        except OSError:
            raise unavailable() from None
        finally:
            temporary.unlink(missing_ok=True)

    def persist(self):
        try:
            self._persist()
        except Exception:
            raise AccessError("Сохранение аккаунта не подтверждено. Повторите вход позже.", "user_persist_unavailable", 503) from None

    def _rate(self, action, login, device, client_ip):
        for bucket, key, limit, window in (
                (action + "-device", device, 5 if action == "register" else 15, 3600 if action == "register" else 900),
                (action + "-ip", client_ip, 10 if action == "register" else 30, 3600 if action == "register" else 900),
                (action + "-target", login, 5 if action == "register" else 15, 3600 if action == "register" else 900),
                (action + "-global", "all", 100 if action == "register" else 60, 3600 if action == "register" else 900)):
            delay = self.ledger.hit_rate("local-" + bucket, key, limit, window)
            if delay:
                self.persist()
                error = AccessError("Слишком много попыток. Повторите позже.", "user_auth_rate_limited", 429)
                error.retry_after = delay
                raise error
        self.persist()

    def session(self, token, *, check_block=True):
        with self.ledger.connect() as db:
            self._integrity(db)
        account = self.ledger.session(token)
        if not account:
            return None
        with self.ledger.connect() as db:
            row = db.execute("SELECT b.guest_id FROM account_guest_bindings b JOIN local_credentials c ON c.account_id=b.account_id WHERE b.account_id=?", (account["id"],)).fetchone()
        if not row:
            raise unavailable()
        if check_block:
            self.guard(account["id"]); self.guard(row[0])
        return {"id": account["id"], "guest_id": row[0], "plan": "free", "email_verified": False, "auth_method": "password"}

    def claimed(self, guest_id):
        with self.ledger.connect() as db:
            self._integrity(db)
            return bool(db.execute("SELECT 1 FROM account_guest_bindings WHERE guest_id=?", (guest_id,)).fetchone())

    def resolve(self, token, guest_id):
        account = self.session(token)
        if account:
            return account
        if self.claimed(guest_id):
            raise AccessError("Для доступа к этим данным войдите в аккаунт.", "account_login_required", 401)
        return None

    def guard_owner_adoption(self, guest_id):
        if self.claimed(guest_id):
            raise AccessError("Это пространство закреплено за обычным аккаунтом. Выйдите из него перед входом владельца.", "account_space_protected", 403)

    def register(self, login, password, guest_id, device, client_ip):
        login = login_name(login); password = _password(password, registering=True)
        AccessLedger._identifier(guest_id, 64)
        if not self.ledger.valid_device(device):
            raise AccessError("Обновите страницу регистрации.", "invalid_device", 403)
        self._rate("register", login, device, client_ip)
        salt = secrets.token_hex(32); digest = password_digest(password, salt)
        account_id = secrets.token_hex(32); login_key = self.ledger.hash_signal(login, "local-login")
        with self.claim(guest_id):
            self.guard(guest_id)
            with self.ledger.connect() as db:
                db.execute("BEGIN IMMEDIATE"); self._integrity(db)
                if (db.execute("SELECT 1 FROM local_credentials WHERE login_key=?", (login_key,)).fetchone()
                        or db.execute("SELECT 1 FROM account_guest_bindings WHERE guest_id=?", (guest_id,)).fetchone()):
                    raise AccessError("Не удалось создать аккаунт. Выберите другой логин или войдите.", "account_registration_conflict", 409)
                db.execute("INSERT INTO accounts VALUES (?,?,'free',?,?)", (account_id, "", self.now(), self.now()))
                db.execute("INSERT INTO local_credentials VALUES (?,?,?,?)", (account_id, login_key, salt, digest))
                db.execute("INSERT INTO account_guest_bindings VALUES (?,?,?)", (account_id, guest_id, self.now()))
                count = db.execute("SELECT count(*) FROM accounts").fetchone()[0]
            self._record_count(count)
        token = self.ledger.create_session(account_id)
        self.persist()
        return token, self.session(token)

    def login(self, login, password, device, client_ip):
        login = login_name(login); password = _password(password)
        if not self.ledger.valid_device(device):
            raise AccessError("Обновите страницу входа.", "invalid_device", 403)
        self._rate("login", login, device, client_ip)
        with self.ledger.connect() as db:
            self._integrity(db)
            row = db.execute("SELECT account_id,salt,digest FROM local_credentials WHERE login_key=?", (self.ledger.hash_signal(login, "local-login"),)).fetchone()
        salt = row["salt"] if row else self.ledger.hash_signal("dummy", "password-salt")
        try:
            candidate = password_digest(password, salt)
        except AccessError:
            raise
        except (ValueError, UnicodeError):
            raise unavailable() from None
        valid = hmac.compare_digest(candidate, row["digest"] if row else "0" * 64)
        if not row or not valid:
            raise AccessError("Неверный логин или пароль.", "user_login_failed", 401)
        with self.ledger.connect() as db:
            bound = db.execute("SELECT guest_id FROM account_guest_bindings WHERE account_id=?", (row["account_id"],)).fetchone()
        if not bound:
            raise unavailable()
        self.guard(row["account_id"]); self.guard(bound[0])
        token = self.ledger.create_session(row["account_id"]); self.persist()
        return token, self.session(token)

    def revoke(self, token):
        self.ledger.revoke_session(token); self.persist()

    def public_bindings(self):
        with self.ledger.connect() as db:
            self._integrity(db)
            return [dict(row) for row in db.execute("SELECT a.id AS account_id,b.guest_id,a.created_at FROM accounts a JOIN local_credentials c ON c.account_id=a.id JOIN account_guest_bindings b ON b.account_id=a.id")]
