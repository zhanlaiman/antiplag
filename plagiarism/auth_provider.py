"""Private email OTP bridge. Provider tokens never enter browser storage."""

from dataclasses import dataclass, field
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class AccessError(RuntimeError):
    def __init__(self, message, code="access_denied", status=403, retry_after=None):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def email_address(value):
    if (not isinstance(value, str) or len(value) > 254 or value != value.strip()
            or not re.fullmatch(r"[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+\.[^\s@\x00-\x1f]+", value)):
        raise AccessError("Укажите действительный адрес электронной почты.", "invalid_email", 400)
    return value.casefold()


@dataclass(frozen=True)
class AuthConfig:
    origin: str
    supabase_url: str
    supabase_key: str = field(repr=False)
    captcha_site_key: str
    captcha_secret: str = field(repr=False)

    def __post_init__(self):
        origin = urlsplit(self.origin)
        loopback = origin.hostname in {"127.0.0.1", "localhost"}
        if (origin.scheme not in ({"http", "https"} if loopback else {"https"}) or not origin.netloc
                or origin.username or origin.password or origin.path or origin.query or origin.fragment):
            raise ValueError("Публичный адрес должен быть HTTPS origin без пути; HTTP доступен только на loopback.")
        provider = urlsplit(self.supabase_url)
        if (provider.scheme != "https" or not provider.hostname or provider.username or provider.password
                or provider.port not in {None, 443} or provider.path or provider.query or provider.fragment
                or not re.fullmatch(r"[a-z0-9-]+\.supabase\.co", provider.hostname)):
            raise ValueError("Нужен HTTPS-адрес проекта Supabase без пути.")
        values = (self.supabase_key, self.captcha_site_key, self.captcha_secret)
        if any(not isinstance(value, str) or not value or any(ord(char) < 33 for char in value) for value in values):
            raise ValueError("Для публичного входа нужны ключи Auth и CAPTCHA.")
        if re.fullmatch(r"[123]x0+[A-Z]{2}", self.captcha_secret) or re.fullmatch(r"[123]x0+[A-Z]{2}", self.captcha_site_key):
            raise ValueError("Тестовые ключи CAPTCHA нельзя использовать для публичного входа.")

    @property
    def secure_cookies(self):
        return urlsplit(self.origin).scheme == "https"

    @classmethod
    def from_env(cls):
        return cls(*(os.environ.get(key, "") for key in (
            "ISTOK_PUBLIC_ORIGIN", "ISTOK_SUPABASE_URL", "ISTOK_SUPABASE_KEY",
            "ISTOK_TURNSTILE_SITE_KEY", "ISTOK_TURNSTILE_SECRET")))


class EmailProvider:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(NoRedirect())

    def _json(self, url, body=None, access_token=None, captcha=False):
        headers = {"Accept": "application/json"}
        if not captcha:
            headers["apikey"] = self.config.supabase_key
        if access_token:
            headers["Authorization"] = "Bearer " + access_token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            request = Request(url, data=data, headers=headers)
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(131_073)
            if len(raw) > 131_072:
                raise ValueError("oversize response")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("invalid response")
            return result
        except HTTPError as error:
            # Never expose a provider body containing tokens or account details.
            error.close()
            if error.code == 429:
                raise AccessError("Слишком много попыток. Попробуйте позже.", "auth_rate_limited", 429, 60) from None
            if error.code in {400, 401, 403, 422}:
                raise AccessError("Не удалось подтвердить вход. Проверьте код или запросите новый.", "invalid_login", 401) from None
            raise AccessError("Сервис входа временно недоступен.", "auth_unavailable", 503) from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise AccessError("Сервис входа временно недоступен.", "auth_unavailable", 503) from None

    def verify_captcha(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 2048:
            raise AccessError("Подтвердите проверку перед отправкой кода.", "captcha_required", 400)
        result = self._json("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                            {"secret": self.config.captcha_secret, "response": token}, captcha=True)
        if (result.get("success") is not True or result.get("hostname") != urlsplit(self.config.origin).hostname
                or result.get("action") != "signin"):
            raise AccessError("Проверка истекла или не подтверждена. Пройдите её ещё раз.", "captcha_invalid", 400)

    def request_code(self, email, captcha_token):
        email = email_address(email)
        self.verify_captcha(captcha_token)
        # CAPTCHA is redeemed here once. Do not forward the used token for a second
        # Siteverify call. Supabase project settings for this server-only bridge
        # are documented separately; no provider key is exposed in the browser.
        self._json(self.config.supabase_url + "/auth/v1/otp", {"email": email, "create_user": True})

    def verify_code(self, email, code):
        email = email_address(email)
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6,8}", code):
            raise AccessError("Введите код из письма.", "invalid_login", 401)
        verified = self._json(self.config.supabase_url + "/auth/v1/verify", {"email": email, "token": code, "type": "email"})
        access_token = verified.get("access_token")
        if not isinstance(access_token, str) or not 20 <= len(access_token) <= 16_000 or any(ord(char) < 33 for char in access_token):
            raise AccessError("Не удалось подтвердить вход.", "invalid_login", 401)
        # Fetch trusted user data from Auth, not from a browser-supplied JWT body
        # or the mutable user_metadata object.
        user = self._json(self.config.supabase_url + "/auth/v1/user", access_token=access_token)
        subject = user.get("id")
        if (not isinstance(subject, str) or not re.fullmatch(r"[a-fA-F0-9-]{36}", subject)
                or not user.get("email_confirmed_at") or user.get("is_anonymous") is True
                or email_address(user.get("email")) != email):
            raise AccessError("Для проверки подтвердите свой адрес электронной почты.", "email_not_verified", 403)
        return {"issuer": self.config.supabase_url + "/auth/v1", "subject": subject.casefold(),
                "email": email, "email_verified": True}
