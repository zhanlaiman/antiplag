"""Beta account transport: existing user session primitives, unchanged guest tenants."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from cloud import dashboard
from plagiarism.access import AccessError
from plagiarism.saas import DEVICE_COOKIE, SESSION_COOKIE
from plagiarism.user_access import UserAccess

STATIC = {"/account": "account.html", "/account/": "account.html", "/account.html": "account.html",
          "/account.js": "account.js", "/account.css": "account.css"}
MAX_BODY = 8192


@dataclass(frozen=True)
class AccountIdentity:
    guest: str
    user: dict | None


def _cookies(headers):
    raw = headers.get("Cookie", "")
    if len(raw) > 16384:
        raise AccessError("Некорректная сессия. Обновите страницу.", "invalid_user_cookie", 400)
    for name in (SESSION_COOKIE, DEVICE_COOKIE, "istok_beta", "istok_owner"):
        if len(re.findall(r"(?:^|;)\s*" + name + "=", raw)) > 1:
            raise AccessError("Неоднозначная сессия.", "invalid_user_cookie", 400)
    try:
        cookies = SimpleCookie(); cookies.load(raw)
        values = {name: cookies[name].value if name in cookies else "" for name in (SESSION_COOKIE, DEVICE_COOKIE)}
        if any(value and not re.fullmatch(r"[A-Za-z0-9_-]{43}", value) for value in values.values()):
            raise AccessError("Некорректная сессия.", "invalid_user_cookie", 400)
        return values
    except (CookieError, ValueError):
        raise AccessError("Некорректная сессия.", "invalid_user_cookie", 400) from None


def handles(path):
    return path == "/api/account" or path.startswith("/api/account/")


class UserAuthRuntime:
    def __init__(self, beta, *, registration_enabled=False, now=None):
        self.beta = beta
        self.registration_enabled = registration_enabled is True
        options = {"now": now} if now is not None else {}
        self.access = UserAccess(beta.root, guard=lambda identifier: dashboard.guard_account(beta, identifier),
                                 claim=self.claim, persist=beta.persist, **options)
        beta.accounts = self

    @contextmanager
    def claim(self, guest_id):
        # Same durable lock/order used by owner_login, which checks guard_owner_adoption.
        try:
            with closing(sqlite3.connect(self.beta.database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("SELECT id FROM guests LIMIT 1").fetchone()
                exists = db.execute("SELECT type FROM sqlite_master WHERE name='owner_account' COLLATE NOCASE").fetchone()
                if exists and exists[0] != "table":
                    raise AccessError("Схема владельца пространства повреждена.", "user_access_unavailable", 503)
                owner = db.execute("SELECT guest FROM owner_account WHERE id=1").fetchone() if exists else None
                if guest_id is not None:
                    if owner and owner[0] == guest_id:
                        raise AccessError("Пространство владельца нельзя зарегистрировать как обычный аккаунт.", "owner_space_protected", 403)
                    self.beta._check_storage()
                yield
        except (OSError, sqlite3.Error):
            raise AccessError("Проверка владельца пространства временно недоступна.", "user_access_unavailable", 503) from None

    def trusted(self, headers, method):
        for name in ("Host", "Origin", "Cookie", "X-Istok-CSRF", "Content-Length", "Content-Type"):
            if hasattr(headers, "get_all") and len(headers.get_all(name) or []) > 1:
                raise AccessError("Неоднозначный запрос.", "user_origin_forbidden", 403)
        if (not self.beta.trusted(headers, method)
                or headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none")):
            raise AccessError("Запрос разрешён только с сайта Истока.", "user_origin_forbidden", 403)

    def cookie(self, name, value, *, expire=False):
        cookie = SimpleCookie(); cookie[name] = value
        cookie[name]["path"] = "/"; cookie[name]["httponly"] = True; cookie[name]["samesite"] = "Lax"
        cookie[name]["max-age"] = 0 if expire else 43200 if name == SESSION_COOKIE else 365 * 86400
        if self.beta.secure:
            cookie[name]["secure"] = True
        return cookie.output(header="").strip()

    def _identity(self, headers, *, create_device=False, client_ip=""):
        values = _cookies(headers); device, token = values[DEVICE_COOKIE], values[SESSION_COOKIE]
        cookie = None
        if not self.access.ledger.valid_device(device):
            device = ""
            if create_device:
                for bucket, key, limit in (("local-bootstrap-ip", client_ip, 120), ("local-bootstrap-global", "all", 500)):
                    delay = self.access.ledger.hit_rate(bucket, key, limit, 3600)
                    if delay:
                        self.access.persist()
                        error = AccessError("Слишком много новых сессий. Повторите позже.", "user_auth_rate_limited", 429)
                        error.retry_after = delay
                        raise error
                device = self.access.ledger.create_device(); self.access.persist()
                cookie = self.cookie(DEVICE_COOKIE, device)
        return device, token, cookie

    def _csrf(self, headers, device, token):
        if not device or not self.access.ledger.verify_csrf(device, headers.get("X-Istok-CSRF"), token):
            raise AccessError("Обновите страницу перед отправкой формы.", "csrf_failed", 403)

    def resolve(self, headers, guest_id):
        values = _cookies(headers)
        user = self.access.resolve(values[SESSION_COOKIE], guest_id)
        return AccountIdentity(user["guest_id"] if user else guest_id, user)

    def guard_owner_adoption(self, guest_id):
        self.access.guard_owner_adoption(guest_id)

    def public_bindings(self):
        return self.access.public_bindings()

    def session_payload(self, headers, client_ip):
        device, token, device_cookie = self._identity(headers, create_device=True, client_ip=client_ip)
        owner = self.beta.owner_identity(headers)
        guest_id, guest_cookie = (owner, None) if owner else self.beta.identity(headers)
        user = self.access.session(token, check_block=False)
        blocked = False
        for identifier in ([user["id"], user["guest_id"]] if user else [guest_id]):
            try:
                dashboard.guard_account(self.beta, identifier)
            except AccessError as error:
                if error.code != "account_blocked":
                    raise
                blocked = True
        claimed = self.access.claimed(guest_id)
        payload = {"authenticated": bool(user), "user": user, "csrf_token": self.access.ledger.csrf(device, token),
                   "owner": bool(owner), "claimed_guest": claimed, "blocked": blocked,
                   "registration_enabled": self.registration_enabled,
                   "registration_allowed": self.registration_enabled and not (owner or user or claimed or blocked),
                   "auth_method": "password", "email_delivery": False, "email_recovery": False}
        return payload, [cookie for cookie in (device_cookie, guest_cookie) if cookie]

    def auth(self, path, body, headers, client_ip):
        device, old_token, _ = self._identity(headers)
        self._csrf(headers, device, old_token)
        if path == "/api/account/logout":
            if body:
                raise AccessError("Некорректные поля запроса.", "invalid_user_request", 400)
            self.access.revoke(old_token)
            guest_id, candidate_cookie = self.beta.identity(headers)
            if candidate_cookie is not None or self.access.claimed(guest_id):
                _, guest_cookie = self.beta.identity({})
            else:
                guest_cookie = None  # Preserve a different browser's unclaimed guest data.
            cookies = [self.cookie(SESSION_COOKIE, "", expire=True)]
            if guest_cookie:
                cookies.append(guest_cookie)
            return {"authenticated": False, "user": None, "csrf_token": self.access.ledger.csrf(device, "")}, cookies
        if path not in {"/api/account/register", "/api/account/login"}:
            raise KeyError(path)
        if set(body) != {"username", "password"}:
            raise AccessError("Некорректные поля запроса.", "invalid_user_request", 400)
        if self.beta.owner_identity(headers):
            raise AccessError("Выйдите из режима владельца перед обычным входом или регистрацией.", "owner_mode_active", 403)
        if path.endswith("/register"):
            if not self.registration_enabled:
                raise AccessError("Создание аккаунтов временно отключено. Вход в существующий аккаунт доступен.", "user_registration_disabled", 503)
            if self.access.session(old_token, check_block=False):
                raise AccessError("Сначала выйдите из текущего аккаунта.", "already_authenticated", 409)
            guest_id, cookie = self.beta.identity(headers)
            if cookie is not None:
                raise AccessError("Откройте страницу регистрации заново.", "verified_guest_required", 401)
            token, user = self.access.register(body["username"], body["password"], guest_id, device, client_ip)
        else:
            token, user = self.access.login(body["username"], body["password"], device, client_ip)
        if old_token:
            self.access.revoke(old_token)
        return {"authenticated": True, "user": user, "csrf_token": self.access.ledger.csrf(device, token)}, [self.cookie(SESSION_COOKIE, token)]


def dispatch(handler, method):
    path = urlsplit(handler.path).path
    if path not in STATIC and not handles(path):
        return False
    dashboard.add_security_headers(handler)
    try:
        runtime = getattr(handler.server, "accounts", None)
        if runtime is None:
            raise AccessError("Регистрация пока недоступна.", "user_auth_unconfigured", 503)
        runtime.trusted(handler.headers, method)
        if path in STATIC:
            if method != "GET":
                raise AccessError("Метод недоступен.", "user_method_not_allowed", 405)
            file = Path(__file__).resolve().parent.parent / "web" / STATIC[path]
            content = file.read_bytes()
            mime = {".html": "text/html", ".js": "application/javascript", ".css": "text/css"}[file.suffix]
            handler.send_response(200); handler.send_header("Content-Type", mime + "; charset=utf-8")
            handler.send_header("Content-Length", str(len(content)))
            for name, value in handler.response_headers:
                handler.send_header(name, value)
            handler.end_headers(); handler.wfile.write(content)
            return True
        if method == "GET" and path == "/api/account/session":
            payload, cookies = runtime.session_payload(handler.headers, handler.client_address[0])
        elif method == "POST":
            length = handler.headers.get("Content-Length", "")
            if (not re.fullmatch(r"[0-9]{1,5}", length) or not 1 <= int(length) <= MAX_BODY
                    or handler.headers.get_content_type() != "application/json" or handler.headers.get("Transfer-Encoding")):
                raise AccessError("Нужен JSON-запрос до 8 КБ.", "invalid_user_request", 400)
            payload, cookies = runtime.auth(path, handler.read_body(), handler.headers, handler.client_address[0])
        else:
            raise KeyError(path)
        for cookie in cookies:
            handler.response_headers.append(("Set-Cookie", cookie))
        handler.send_json(payload)
    except AccessError as error:
        if getattr(error, "retry_after", None):
            handler.response_headers.append(("Retry-After", str(error.retry_after)))
        handler.send_json({"error": str(error), "code": error.code}, error.status)
    except KeyError:
        handler.send_json({"error": "Маршрут не найден.", "code": "user_route_not_found"}, 404)
    except (ValueError, UnicodeError, json.JSONDecodeError):
        handler.send_json({"error": "Некорректный запрос.", "code": "invalid_user_request"}, 400)
    except Exception:
        handler.send_json({"error": "Вход временно недоступен. Повторите позже.", "code": "user_auth_unavailable"}, 503)
    return True
