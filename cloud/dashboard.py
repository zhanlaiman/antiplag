"""Separate administrator boundary. Never grants authority from beta/owner/API cookies."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import hmac
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from urllib.parse import parse_qs, urlsplit

from plagiarism.access import AccessError
from plagiarism.user_accounts import AccountInventory, valid_account_id
from plagiarism.password_auth import password_digest, SCRYPT_N, SCRYPT_R, SCRYPT_P


COOKIE = "istok_dashboard_admin"
SESSION_SECONDS = 3600
LOGIN_WINDOW = 900
LOGIN_IP_LIMIT = 10
LOGIN_GLOBAL_LIMIT = 40
MAX_BODY = 8192
STATIC = {"/dashboard": "dashboard.html", "/dashboard/": "dashboard.html",
          "/dashboard.html": "dashboard.html", "/dashboard.js": "dashboard.js",
          "/dashboard.css": "dashboard.css"}
POOL_ERRORS = {
    "pool_invalid_input": (400, "Проверьте поля настройки поискового ключа."),
    "pool_body_too_large": (413, "Запрос настройки ключа слишком большой."),
    "pool_credential_missing": (404, "Сохранённый ключ не найден. Обновите панель."),
    "pool_check_required": (409, "Сначала проверьте сохранённый ключ."),
    "pool_check_rate_limited": (429, "Проверка ключей временно ограничена. Повторите позже."),
    "pool_grant_required": (409, "Для активации требуется подтверждение доступа."),
    "pool_recovery_required": (503, "Хранилище ключей требует восстановления."),
    "pool_storage_unavailable": (503, "Хранилище поисковых ключей временно недоступно."),
    "pool_storage_capacity": (503, "В хранилище поисковых ключей нет места."),
    "pool_method_not_allowed": (405, "Метод настройки ключа недоступен."),
    "pool_not_found": (404, "Маршрут настройки ключа не найден."),
    "pool_unavailable": (503, "Управление поисковыми ключами временно недоступно."),
}


@dataclass(frozen=True)
class DashboardConfig:
    username: str = field(repr=False)
    salt: str = field(repr=False)
    digest: str = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.username, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", self.username)
                or not isinstance(self.salt, str) or not re.fullmatch(r"[a-f0-9]{64}", self.salt)
                or not isinstance(self.digest, str) or not re.fullmatch(r"[a-f0-9]{64}", self.digest)):
            raise ValueError("Invalid dashboard hash configuration")

    @property
    def fingerprint(self):
        return sha256((self.username + "\0" + self.salt + "\0" + self.digest).encode()).hexdigest()

    @classmethod
    def from_env(cls):
        path = os.environ.get("ISTOK_DASHBOARD_CONFIG")
        if path:
            # Raw credential handoff files are intentionally rejected by strict keys.
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_BODY:
                    raise ValueError("Dashboard config must be a private regular file")
                values = json.load(stream)
            if not isinstance(values, dict) or set(values) != {"username", "salt", "digest"}:
                raise ValueError("Dashboard config accepts hash fields only")
            return cls(**values)
        values = [os.environ.get("ISTOK_DASHBOARD_" + key, "") for key in ("USERNAME", "SALT", "DIGEST")]
        return cls(*values) if any(values) else None


def handles(path):
    return path == "/api/dashboard" or path.startswith("/api/dashboard/")


def _fields(body, names):
    if not isinstance(body, dict) or set(body) != set(names):
        raise AccessError("Некорректные поля запроса.", "dashboard_invalid_request", 400)


def _token(headers):
    raw = headers.get("Cookie", "")
    if len(raw) > 16384 or len(re.findall(r"(?:^|;)\s*" + COOKIE + "=", raw)) != 1:
        return None
    try:
        cookies = SimpleCookie(); cookies.load(raw)
        value = cookies[COOKIE].value
        return value if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) else None
    except (KeyError, CookieError, ValueError):
        return None


class DashboardRuntime:
    def __init__(self, beta, config=None, *, account_ledger=None, pool_route=None, business_writes_enabled=True, now=time.time):
        if type(business_writes_enabled) is not bool:
            raise ValueError("Dashboard write mode must be an explicit boolean")
        self.business_writes_enabled = business_writes_enabled
        self.beta, self.config, self.pool_route, self.now = beta, config, pool_route, now
        private = beta.root / "private"
        if private.is_symlink():
            raise ValueError("Dashboard directory cannot be a symlink")
        private.mkdir(mode=0o700, exist_ok=True)
        self.database = private / "dashboard.sqlite3"
        marker = beta.runtime / "dashboard-initialized"
        if marker.is_symlink() or (marker.exists() and not self.database.exists()):
            raise AccessError("База доступа отсутствует. Восстановите её из резервной копии.", "account_control_unavailable", 503)
        existed = self.database.exists()
        descriptor = os.open(self.database, os.O_RDWR | os.O_NOFOLLOW | (0 if existed else os.O_CREAT | os.O_EXCL), 0o600)
        os.fchmod(descriptor, 0o600); os.close(descriptor)
        with self.connect() as db:
            if existed:
                required = {
                    "admin_sessions": {"token_hash", "config_hash", "expires_at"},
                    "admin_login_attempts": {"ip_hash", "at"},
                    "account_blocks": {"account_id", "blocked", "reason", "changed_at"},
                    "admin_audit": {"id", "at", "action", "account_id", "reason"},
                }
                for table, columns in required.items():
                    kind = db.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
                    actual = {row["name"] for row in db.execute("PRAGMA table_info(" + table + ")")}
                    if not kind or kind[0] != "table" or actual != columns:
                        raise AccessError("Схема базы доступа повреждена. Восстановите резервную копию.", "account_control_unavailable", 503)
            else:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_sessions(
                    token_hash TEXT PRIMARY KEY, config_hash TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS admin_login_attempts(ip_hash TEXT NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS admin_attempt_time ON admin_login_attempts(at);
                CREATE TABLE IF NOT EXISTS account_blocks(
                    account_id TEXT PRIMARY KEY, blocked INTEGER NOT NULL CHECK(blocked IN (0,1)),
                    reason TEXT NOT NULL, changed_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS admin_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, action TEXT NOT NULL,
                    account_id TEXT, reason TEXT NOT NULL);
            """)
            if config is None:
                db.execute("DELETE FROM admin_sessions")
            else:
                # A config rollback must not resurrect cookies revoked by rotation.
                db.execute("DELETE FROM admin_sessions WHERE config_hash!=? OR expires_at<=?", (config.fingerprint, now()))
        try:
            with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as stream:
                stream.write("dashboard-v1\n"); stream.flush(); os.fsync(stream.fileno())
        except FileExistsError:
            pass
        self.inventory = AccountInventory(beta, account_ledger)
        beta.dashboard = self
        self.persist()

    @contextmanager
    def connect(self, *, readonly=False):
        try:
            if self.database.is_symlink():
                raise OSError("symlink")
            uri = self.database.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw")
            db = sqlite3.connect(uri, uri=True, timeout=3)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            raise AccessError("Панель временно недоступна. Повторите позже.", "dashboard_unavailable", 503) from None

    def persist(self):
        try:
            self.beta.persist()
        except Exception:
            raise AccessError("Изменение не подтверждено хранилищем. Обновите панель.", "dashboard_persist_unavailable", 503) from None

    def trusted(self, headers, *, mutating=False):
        for name in ("Host", "Origin", "Cookie", "X-CSRF-Token", "Content-Length", "Content-Type"):
            if hasattr(headers, "get_all") and len(headers.get_all(name) or []) > 1:
                raise AccessError("Неоднозначный защищённый запрос.", "dashboard_origin_forbidden", 403)
        if (not self.beta.trusted(headers, "POST" if mutating else "GET")
                or headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin")):
            raise AccessError("Запрос разрешён только с сайта Истока.", "dashboard_origin_forbidden", 403)

    def cookie(self, token=""):
        return (COOKIE + "=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age="
                + str(SESSION_SECONDS if token else 0) + ("; Secure" if self.beta.secure else ""))

    def csrf(self, token):
        return hmac.new(bytes.fromhex(self.config.digest), ("dashboard-csrf\0" + token).encode(), sha256).hexdigest()

    def authorize(self, headers, *, mutating=False):
        self.trusted(headers, mutating=mutating)
        if self.config is None:
            raise AccessError("Вход администратора ещё не настроен.", "dashboard_unconfigured", 503)
        token = _token(headers)
        if token:
            with self.connect(readonly=True) as db:
                row = db.execute("SELECT expires_at FROM admin_sessions WHERE token_hash=? AND config_hash=? AND expires_at>?",
                                 (sha256(token.encode()).hexdigest(), self.config.fingerprint, self.now())).fetchone()
            if row:
                if mutating:
                    value = headers.get("X-CSRF-Token", "")
                    if not re.fullmatch(r"[a-f0-9]{64}", value) or not hmac.compare_digest(self.csrf(token), value):
                        raise AccessError("Обновите панель и повторите действие.", "dashboard_csrf_failed", 403)
                return {"authenticated": True, "csrf_token": self.csrf(token), "expires_at": row[0], "business_writes_enabled": self.business_writes_enabled}
        raise AccessError("Войдите в панель администратора.", "dashboard_auth_required", 401)

    def login(self, headers, body, client_ip):
        self.trusted(headers, mutating=True)
        if self.config is None:
            raise AccessError("Вход администратора ещё не настроен.", "dashboard_unconfigured", 503)
        _fields(body, ("username", "password"))
        login, password = body["username"], body["password"]
        if not isinstance(login, str) or not isinstance(password, str) or len(login) > 96 or len(password) > 256:
            raise AccessError("Некорректные данные входа.", "dashboard_invalid_request", 400)
        now = self.now()
        ip_hash = hmac.new(bytes.fromhex(self.config.digest), str(client_ip).encode(), sha256).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM admin_login_attempts WHERE at<=?", (now - LOGIN_WINDOW,))
            total, per_ip = db.execute("SELECT count(*),coalesce(sum(ip_hash=?),0) FROM admin_login_attempts", (ip_hash,)).fetchone()
            if total >= LOGIN_GLOBAL_LIMIT or per_ip >= LOGIN_IP_LIMIT:
                error = AccessError("Слишком много попыток. Повторите через 15 минут.", "dashboard_rate_limited", 429)
                error.retry_after = LOGIN_WINDOW
                raise error
            db.execute("INSERT INTO admin_login_attempts VALUES (?,?)", (ip_hash, now))
        self.persist()
        try:
            candidate = password_digest(password, self.config.salt)
            valid = hmac.compare_digest(login.encode(), self.config.username.encode()) & hmac.compare_digest(candidate, self.config.digest)
        except UnicodeError:
            valid = False
        if not valid:
            raise AccessError("Неверный логин или пароль.", "dashboard_login_failed", 401)
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE expires_at<=? OR config_hash!=?", (now, self.config.fingerprint))
            old = _token(headers)
            if old:
                db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (sha256(old.encode()).hexdigest(),))
            db.execute("INSERT INTO admin_sessions VALUES (?,?,?)", (sha256(token.encode()).hexdigest(), self.config.fingerprint, now + SESSION_SECONDS))
            db.execute("INSERT INTO admin_audit(at,action,reason) VALUES (?,'login','')", (now,))
        self.persist()
        return {"authenticated": True, "csrf_token": self.csrf(token), "expires_at": now + SESSION_SECONDS, "business_writes_enabled": self.business_writes_enabled}, self.cookie(token)

    def guard_business(self, method, path):
        mutation = (re.fullmatch(r"/api/dashboard/accounts/[a-f0-9]{64}/(?:block|unblock)", path)
                    or path == "/api/dashboard/pool" or path.startswith("/api/dashboard/pool/"))
        if method == "POST" and mutation and not self.business_writes_enabled:
            raise AccessError("Панель доступна только для просмотра. Изменения временно отключены.", "dashboard_readonly", 503)

    def logout(self, headers):
        self.authorize(headers, mutating=True)
        with self.connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (sha256(_token(headers).encode()).hexdigest(),))
            db.execute("INSERT INTO admin_audit(at,action,reason) VALUES (?,'logout','')", (self.now(),))
        self.persist()
        return {"authenticated": False}, self.cookie()

    def guard_account(self, account_id):
        valid_account_id(account_id)
        _, aliases = self.inventory.canonical_id(account_id)
        with self.connect(readonly=True) as db:
            rows = db.execute("SELECT blocked FROM account_blocks WHERE account_id IN (" + ",".join("?" for _ in aliases) + ")", aliases).fetchall()
        if any(row[0] for row in rows):
            raise AccessError("Доступ аккаунта приостановлен администратором.", "account_blocked", 403)

    def accounts(self, offset=0):
        with self.connect(readonly=True) as db:
            controls = {row["account_id"]: dict(row) for row in db.execute("SELECT account_id,blocked,reason,changed_at FROM account_blocks")}
        return self.inventory.page(controls, offset=offset)

    def set_block(self, account_id, blocked, reason):
        if not self.business_writes_enabled:
            raise AccessError("Панель доступна только для просмотра. Изменения временно отключены.", "dashboard_readonly", 503)
        valid_account_id(account_id)
        account_id, aliases = self.inventory.canonical_id(account_id)
        if (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 240
                or any(ord(char) < 32 for char in reason)):
            raise AccessError("Укажите причину длиной до 240 символов.", "dashboard_invalid_request", 400)
        # Inventory reads only IDs, never tenant contents, to reject arbitrary targets.
        if account_id not in self.inventory.identities()[0]:
            with self.connect(readonly=True) as db:
                known = db.execute("SELECT 1 FROM account_blocks WHERE account_id=?", (account_id,)).fetchone()
            if not known:
                raise KeyError(account_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for identifier in aliases:
                db.execute("INSERT INTO account_blocks VALUES (?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET blocked=excluded.blocked,reason=excluded.reason,changed_at=excluded.changed_at",
                           (identifier, int(blocked), reason.strip(), self.now()))
            db.execute("INSERT INTO admin_audit(at,action,account_id,reason) VALUES (?,?,?,?)",
                       (self.now(), "block" if blocked else "unblock", account_id, reason.strip()))
        self.persist()
        return {"account_id": account_id, "blocked": blocked}

    def audit(self, offset=0):
        with self.connect(readonly=True) as db:
            total = db.execute("SELECT count(*) FROM admin_audit").fetchone()[0]
            rows = [dict(row) for row in db.execute("SELECT id,at,action,account_id,reason FROM admin_audit ORDER BY id DESC LIMIT 50 OFFSET ?", (offset,))]
        return {"events": rows, "total": total, "offset": offset, "limit": 50}


def _runtime(handler):
    runtime = getattr(handler.server, "dashboard", None)
    if runtime is None:
        raise AccessError("Панель администратора недоступна.", "dashboard_unconfigured", 503)
    return runtime


def require_admin(handler, method):
    handler.dashboard_admin = _runtime(handler).authorize(handler.headers, mutating=method not in {"GET", "HEAD"})
    return handler.dashboard_admin


def guard_account(beta, account_id):
    runtime = getattr(beta, "dashboard", None)
    if runtime is None:
        raise AccessError("Проверка доступа временно недоступна.", "account_control_unavailable", 503)
    runtime.guard_account(account_id)


def _offset(handler):
    query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
    if set(query) - {"offset"} or len(query.get("offset", ["0"])) != 1 or not re.fullmatch(r"[0-9]{1,7}", query.get("offset", ["0"])[0]):
        raise AccessError("Некорректная страница.", "dashboard_invalid_request", 400)
    return int(query.get("offset", ["0"])[0])


def route(handler, method, path, body):
    if not handles(path):
        raise KeyError(path)
    runtime = _runtime(handler)
    if method == "POST" and path == "/api/dashboard/login":
        payload, cookie = runtime.login(handler.headers, body, handler.client_address[0])
        handler.response_headers.append(("Set-Cookie", cookie))
        return payload
    if method == "GET" and path == "/api/dashboard/session":
        runtime.trusted(handler.headers)
        try:
            return runtime.authorize(handler.headers)
        except AccessError as error:
            if error.code == "dashboard_auth_required":
                return {"authenticated": False, "configured": True}
            if error.code == "dashboard_unconfigured":
                return {"authenticated": False, "configured": False}
            raise
    require_admin(handler, method)
    runtime.guard_business(method, path)
    if method == "POST" and path == "/api/dashboard/logout":
        _fields(body, ())
        payload, cookie = runtime.logout(handler.headers)
        handler.response_headers.append(("Set-Cookie", cookie))
        return payload
    if method == "GET" and path == "/api/dashboard/accounts":
        return runtime.accounts(_offset(handler))
    if method == "GET" and path == "/api/dashboard/audit":
        return runtime.audit(_offset(handler))
    match = re.fullmatch(r"/api/dashboard/accounts/([a-f0-9]{64})/(block|unblock)", path)
    if method == "POST" and match:
        _fields(body, ("reason",))
        return runtime.set_block(match[1], match[2] == "block", body["reason"])
    if (path == "/api/dashboard/pool" or path.startswith("/api/dashboard/pool/")) and runtime.pool_route:
        try:
            return runtime.pool_route(handler, method, path, body, authorize=require_admin)
        except AccessError as error:
            # Provider implementations must not choose public messages containing keys.
            if isinstance(error.code, str) and error.code in POOL_ERRORS:
                status, message = POOL_ERRORS[error.code]
                raise AccessError(message, error.code, status) from None
            status = error.status if error.status in {400, 401, 403, 404, 409, 429, 503} else 503
            raise AccessError("Операция с поисковыми ключами не выполнена. Обновите панель.", "dashboard_pool_request_failed", status) from None
    raise KeyError(path)


def add_security_headers(handler):
    handler.response_headers.extend([
        ("Cache-Control", "no-store"), ("X-Robots-Tag", "noindex, nofollow"),
        ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
        ("X-Frame-Options", "DENY"),
        ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ])
    if getattr(handler.server.beta, "secure", False):
        handler.response_headers.append(("Strict-Transport-Security", "max-age=31536000; includeSubDomains"))


def dispatch(handler, method):
    """Call first in BetaHandler._dispatch. True means no guest handling may follow."""
    path = urlsplit(handler.path).path
    if not handles(path) and path not in STATIC:
        return False
    add_security_headers(handler)
    try:
        runtime = _runtime(handler)
        runtime.trusted(handler.headers, mutating=method not in {"GET", "HEAD"})
        if method not in {"GET", "POST"}:
            require_admin(handler, method)
            raise AccessError("Метод недоступен.", "dashboard_method_not_allowed", 405)
        if path in STATIC:
            if method != "GET":
                raise AccessError("Метод недоступен.", "dashboard_method_not_allowed", 405)
            file = Path(__file__).resolve().parent.parent / "web" / STATIC[path]
            content = file.read_bytes()
            handler.send_response(200)
            mime = "text/html" if file.suffix == ".html" else "application/javascript" if file.suffix == ".js" else "text/css"
            handler.send_header("Content-Type", mime + "; charset=utf-8")
            handler.send_header("Content-Length", str(len(content)))
            for name, value in handler.response_headers:
                handler.send_header(name, value)
            handler.end_headers(); handler.wfile.write(content)
            return True
        body = {}
        if method not in {"GET", "HEAD"}:
            # Authenticate protected mutations before reading their body or touching pool.
            if not (method == "POST" and path == "/api/dashboard/login"):
                require_admin(handler, method)
                runtime.guard_business(method, path)
            length = handler.headers.get("Content-Length", "")
            if (not re.fullmatch(r"[0-9]{1,5}", length) or not 1 <= int(length) <= MAX_BODY
                    or handler.headers.get_content_type() != "application/json"
                    or handler.headers.get("Transfer-Encoding")):
                raise AccessError("Нужен JSON-запрос до 8 КБ.", "dashboard_invalid_request", 400)
            body = handler.read_body()
        handler.send_json(route(handler, method, path, body))
    except AccessError as error:
        if getattr(error, "retry_after", None):
            handler.response_headers.append(("Retry-After", str(error.retry_after)))
        handler.send_json({"error": str(error), "code": error.code}, error.status)
    except KeyError:
        handler.send_json({"error": "Маршрут не найден.", "code": "dashboard_not_found"}, 404)
    except (ValueError, UnicodeError):
        handler.send_json({"error": "Некорректный JSON-запрос.", "code": "dashboard_invalid_request"}, 400)
    except Exception:
        # Pool/provider exceptions may embed keys. Keep this outer boundary constant.
        handler.send_json({"error": "Панель временно недоступна.", "code": "dashboard_request_failed"}, 503)
    return True
