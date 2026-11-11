"""Optional account boundary and admission policy for the public HTTP app."""

from dataclasses import dataclass
from hashlib import sha256
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from plagiarism import access, auth_provider, learning, source_packs, web_search
from plagiarism.tenants import TenantRegistry, PUBLIC_PACK_IDS


DEVICE_COOKIE = "istok_device"
SESSION_COOKIE = "istok_session"


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def report_identifier(request_id):
    access.AccessLedger._identifier(request_id)
    return sha256(("report\0" + request_id).encode("ascii")).hexdigest()[:32]


def ensure_job_identifier(store, request_id):
    access.AccessLedger._identifier(request_id)
    with store.connect() as connection:
        collision = connection.execute("SELECT kind FROM records WHERE id=? AND kind!='model-job'", (request_id,)).fetchone()
    if collision:
        raise access.AccessError("Идентификатор занят другой записью. Создайте новый запрос.", "request_conflict", 409)


@dataclass(frozen=True)
class RequestIdentity:
    device: str
    session_token: str
    user: dict | None


class SaaSRuntime:
    def __init__(self, config, ledger, tenants, provider=None, learning_queue=None):
        self.config, self.ledger, self.tenants = config, ledger, tenants
        self.provider = provider if provider is not None else auth_provider.EmailProvider(config)
        self.learning = learning_queue
        self.upload_lock = Lock()
        self.public_source_ids = {item["id"] for pack in PUBLIC_PACK_IDS
                                  for item in source_packs.load_pack(pack)["sources"]}

    @classmethod
    def from_env(cls, root=None):
        config = auth_provider.AuthConfig.from_env()
        root = Path(root if root is not None else os.environ.get("ISTOK_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
        return cls(config, access.AccessLedger(root / "private" / "access.sqlite3"),
                   TenantRegistry(root / "tenants"), learning_queue=learning.LearningQueue(root / "private" / "learning.sqlite3"))

    def trusted(self, headers, method):
        origin = headers.get("Origin")
        return (headers.get("Host") == urlsplit(self.config.origin).netloc
                and (origin == self.config.origin if method not in {"GET", "HEAD"}
                     else origin is None or origin == self.config.origin))

    def identity(self, headers, *, create_device=False):
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get("Cookie", ""))
        except CookieError:
            cookies = SimpleCookie()
        device = cookies[DEVICE_COOKIE].value if DEVICE_COOKIE in cookies else ""
        session = cookies[SESSION_COOKIE].value if SESSION_COOKIE in cookies else ""
        new_device = False
        if not self.ledger.valid_device(device):
            device = self.ledger.create_device() if create_device else ""
            new_device = bool(device)
        return RequestIdentity(device, session, self.ledger.session(session)), new_device

    def cookie(self, name, value, *, expire=False):
        cookie = SimpleCookie()
        cookie[name] = value
        cookie[name]["path"] = "/"
        cookie[name]["httponly"] = True
        cookie[name]["samesite"] = "Lax"
        cookie[name]["max-age"] = 0 if expire else (43200 if name == SESSION_COOKIE else 365 * 86400)
        if self.config.secure_cookies:
            cookie[name]["secure"] = True
        return cookie.output(header="").strip()

    def csrf_guard(self, identity, headers):
        if not identity.device or not self.ledger.verify_csrf(
                identity.device, headers.get("X-Istok-CSRF"), identity.session_token):
            raise access.AccessError("Обновите страницу перед отправкой защищённого запроса.", "csrf_failed", 403)

    @staticmethod
    def require_user(identity):
        if identity.user is None:
            raise access.AccessError("Войдите и подтвердите электронную почту.", "authentication_required", 401)
        return identity.user

    def usage(self, identity):
        return self.ledger.usage(self.require_user(identity)["id"], identity.device)

    def session_payload(self, identity):
        usage = self.usage(identity) if identity.user else None
        return {"mode": "saas", "user": identity.user, "web": web_search.status(),
                "csrf_token": self.ledger.csrf(identity.device, identity.session_token),
                "auth": {"configured": True, "method": "email_otp", "captcha_site_key": self.config.captcha_site_key,
                         "status_kind": "configuration"},
                "quota": ({"limit": usage["limits"]["characters"], "used": usage["used"]["characters"],
                           "reserved": None, "allocated": usage["used"]["characters"],
                           "accounting": "allocated_includes_pending", "remaining": usage["remaining"]["characters"],
                           "reset_at": usage["reset_at"]}
                          if usage else None), "usage": usage}

    def _rate(self, bucket, key, limit, window):
        delay = self.ledger.hit_rate(bucket, key, limit, window)
        if delay:
            raise auth_provider.AccessError("Слишком много попыток. Попробуйте позже.", "auth_rate_limited", 429, delay)

    def auth(self, path, body, identity, client_ip):
        if path == "/api/auth/request":
            email = auth_provider.email_address(body.get("email"))
            self._rate("email-request", access.canonical_email(email), 3, 600)
            self._rate("device-request", identity.device, 5, 600)
            self._rate("ip-request", client_ip, 30, 600)
            self.provider.request_code(email, body.get("captcha_token"))
            return {"challenge_id": self.ledger.create_challenge(email, identity.device)}, None
        if path == "/api/auth/verify":
            email = auth_provider.email_address(body.get("email"))
            self._rate("device-verify", identity.device, 20, 600)
            self._rate("ip-verify", client_ip, 60, 600)
            challenge = body.get("challenge_id")
            if not self.ledger.check_challenge(challenge, email, identity.device):
                raise auth_provider.AccessError("Запрос кода истёк. Запросите новый код.", "invalid_login", 401)
            trusted = self.provider.verify_code(email, body.get("code"))
            if not self.ledger.consume_challenge(challenge):
                raise auth_provider.AccessError("Этот код уже использован. Запросите новый.", "invalid_login", 401)
            user = self.ledger.ensure_identity(trusted["issuer"], trusted["subject"], trusted["email"], trusted["email_verified"])
            return {"verified": True}, self.cookie(SESSION_COOKIE, self.ledger.create_session(user["id"]))
        if path == "/api/auth/logout":
            self.ledger.revoke_session(identity.session_token)
            return {"logged_out": True}, self.cookie(SESSION_COOKIE, "", expire=True)
        raise KeyError(path)

    def add_source(self, store, title, text, url):
        if len(text) > 20000:
            raise access.AccessError("В бесплатном плане один свой источник содержит до 20000 символов.", "source_size_limit", 400)
        with self.upload_lock:
            custom = [item for item in store.sources() if item["id"] not in self.public_source_ids]
            if len(custom) >= 10 or sum(len(item["text"]) for item in custom) + len(text) > 200000:
                raise access.AccessError("Доступно до десяти своих источников и 200000 символов суммарно.", "source_library_limit", 429)
            return store.add_source(title, text, url)

    def reused_job(self, jobs, store, account_id, reservation):
        if not reservation["reused"]:
            return None
        try:
            job = jobs.get(reservation["request_id"], store)
        except KeyError:
            raise access.AccessError("Исход предыдущего запуска не подтверждён. Автоматический повтор отключён.",
                                     "operation_uncertain", 409) from None
        self.reconcile_job(account_id, job)
        return job

    def reconcile_job(self, account_id, job):
        if job.get("status") in {"cancelled", "failed"}:
            try:
                self.ledger.release(account_id, job["id"])
            except access.AccessError:
                pass  # A started operation cannot receive an automatic refund.

    def queued_operation(self, jobs, store, account_id, request_id, path, body, report_id, kind,
                         operation, candidate_attempt=None, *, device_id, model_ops_cost=1):
        ensure_job_identifier(store, request_id)
        reservation = self.ledger.reserve_operation(account_id, report_id, request_id, kind,
            fingerprint({"path": path, "body": body}), device_id=device_id, model_ops_cost=model_ops_cost)
        reused = self.reused_job(jobs, store, account_id, reservation)
        if reused is not None:
            return reused

        def admitted(progress):
            self.ledger.mark_started(account_id, request_id)
            try:
                return operation(progress)
            finally:
                self.ledger.commit(account_id, request_id)

        try:
            return jobs.submit_model(request_id, path, body, admitted, store, candidate_attempt=candidate_attempt)
        except BaseException:
            try:
                self.ledger.release(account_id, request_id)
            except access.AccessError:
                pass
            raise

    def close(self):
        self.tenants.close()
