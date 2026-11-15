"""Owner-only search pool service. No visitor search routing or grant creation.

See docs/SEARCH_POOL_ADMIN.md for the private runtime and HTTP integration seam.
Raw provider JSON is parsed once and never returned, audited or logged.
"""

from copy import deepcopy
from hashlib import sha256
import hmac
import json
import math
import re
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from plagiarism.access import AccessError
from plagiarism.auth_provider import NoRedirect
from plagiarism.search_key_vault import KeyVault, MAX_CREDENTIALS, vault_error


PROVIDERS = ("serpapi", "tavily", "serper", "exa")
LIVE_PROVIDERS = ("serpapi", "tavily")
MAX_KEY_BYTES = 1024
MAX_BODY_BYTES = 4096
MAX_USAGE_BYTES = 65536
USAGE_TIMEOUT = 12
CHECK_TTL = 300
CHECK_COOLDOWN = 3
MONTHLY_LIMIT = 180
ERROR_CODES = {"quota_unconfirmed", "provider_unavailable", "quota_exhausted",
               "balance_unconfirmed", "free_verified", "not_checked"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def fail(code, status=400):
    messages = {
        "pool_invalid_input": "Проверьте провайдера, ключ и поля запроса.",
        "pool_body_too_large": "Запрос слишком большой. Ключ — не более 1024 символов.",
        "pool_credential_missing": "Сохранённый ключ не найден.",
        "pool_check_required": "Сначала подтвердите бесплатную квоту. Проверка действует пять минут.",
        "pool_check_rate_limited": "Подождите несколько секунд перед повторной проверкой.",
        "pool_grant_required": "Нужен подтверждённый сервером бесплатный бюджет для этого ключа и аккаунта.",
        "pool_admin_required": "Войдите в панель администратора.",
        "pool_unavailable": "Пул поиска сейчас недоступен. Повторите запрос позже.",
        "pool_method_not_allowed": "Для изменения пула используйте POST.",
        "pool_not_found": "Раздел управления пулом не найден.",
    }
    return AccessError(messages[code], code, status)


def _integer(value):
    return type(value) is int and value >= 0


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _provider(value):
    if not isinstance(value, str) or value not in PROVIDERS:
        raise fail("pool_invalid_input")
    return value


def _key(provider, value):
    if (not isinstance(value, str) or not 16 <= len(value) <= MAX_KEY_BYTES
            or any(not 33 <= ord(char) <= 126 for char in value)
            or provider == "tavily" and not value.startswith("tvly-")):
        raise fail("pool_invalid_input")
    return value


def _credential_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise fail("pool_invalid_input")
    return value


def _digest(state, namespace, value):
    return hmac.new(bytes.fromhex(state["salt"]), (namespace + "\0" + value).encode(), sha256).hexdigest()


def _verification(code="not_checked", checked_at=None, **fields):
    return {"code": code, "checked_at": checked_at, "provider_remaining": None,
            "balance_source": "unconfirmed", "free_verified": False, **fields}


def probe_usage(provider, api_key):
    """Only documented, read-only account endpoints with the current search key."""
    if provider == "serpapi":
        request = Request("https://serpapi.com/account.json?" + urlencode({"api_key": api_key}),
                          headers={"Accept": "application/json"})
    elif provider == "tavily":
        request = Request("https://api.tavily.com/usage",
                          headers={"Accept": "application/json", "Authorization": "Bearer " + api_key})
    else:
        raise ValueError("No confirmed usage endpoint")
    try:
        with build_opener(NoRedirect()).open(request, timeout=USAGE_TIMEOUT) as response:
            if response.status != 200:
                raise ValueError("Unexpected status")
            raw = response.read(MAX_USAGE_BYTES + 1)
        if len(raw) > MAX_USAGE_BYTES:
            raise ValueError("Response cap")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid response")
        return value
    except HTTPError as error:
        error.close()
        raise ValueError("Usage unavailable") from None
    except Exception:
        # SerpApi authenticates in a URL; even a transport exception's repr
        # can disclose the key. Never propagate it to the generic server log.
        raise ValueError("Usage unavailable") from None


def _parse_usage(provider, value, checked_at, state):
    rejected = _verification("quota_unconfirmed", checked_at)
    if not isinstance(value, dict) or any(field in value for field in ("error", "detail")):
        return rejected, None
    if provider == "serpapi":
        names = ("searches_per_month", "plan_searches_left", "extra_credits",
                 "total_searches_left", "this_month_usage")
        if (value.get("account_status") != "Active" or value.get("plan_name") != "Free Plan"
                or not _number(value.get("plan_monthly_price")) or value["plan_monthly_price"] != 0
                or any(not _integer(value.get(name)) for name in names)
                or value["searches_per_month"] != 250 or value["extra_credits"] != 0
                or not 0 <= value["this_month_usage"] <= 250
                or not 0 <= value["total_searches_left"] == value["plan_searches_left"] <= 250
                or value["plan_searches_left"] + value["this_month_usage"] != 250):
            return rejected, None
        remaining = value["plan_searches_left"]
        account_id = value.get("account_id")
        account_ref = ("serpapi:" + _digest(state, "account:serpapi", account_id)
                       if isinstance(account_id, str) and IDENTIFIER.fullmatch(account_id) else None)
        return _verification("free_verified" if remaining else "quota_exhausted", checked_at,
                             free_verified=True, balance_source="provider_live", provider_remaining=remaining,
                             plan_limit=250, plan_used=value["this_month_usage"], guard_remaining=remaining), account_ref
    account, key = value.get("account"), value.get("key")
    if (not isinstance(account, dict) or not isinstance(key, dict)
            or account.get("current_plan") != "Researcher"
            or any(not _integer(account.get(name)) for name in ("plan_usage", "plan_limit", "paygo_usage", "paygo_limit"))
            or account["plan_limit"] != 1000 or account["paygo_usage"] != 0 or account["paygo_limit"] != 0
            or account["plan_usage"] > 1000
            or not _integer(key.get("usage")) or key["usage"] > 1000
            or key["usage"] > account["plan_usage"]
            or not _integer(key.get("limit")) or not 1 <= key["limit"] <= 1000
            or key["usage"] > key["limit"]):
        return rejected, None
    remaining = 1000 - account["plan_usage"]
    guard_remaining = max(0, min(900 - account["plan_usage"], key["limit"] - key["usage"]))
    return _verification("free_verified" if guard_remaining else "quota_exhausted", checked_at,
                         free_verified=True, balance_source="provider_live", provider_remaining=remaining,
                         plan_limit=1000, plan_used=account["plan_usage"], guard_remaining=guard_remaining), None


class PoolAdmin:
    def __init__(self, root, persist=lambda: None, *, usage_probe=None,
                 runtime_status=None, activation_policy=None, clock=time.time):
        self.vault = KeyVault(root, persist)
        self.usage_probe = usage_probe or probe_usage
        self.runtime_status = runtime_status
        self.activation_policy = activation_policy
        self.clock = clock

    def _record(self, state, identifier):
        _credential_id(identifier)
        row = state["records"].get(identifier)
        if row is None:
            raise fail("pool_credential_missing", 404)
        try:
            if not isinstance(row, dict) or row["credential_id"] != identifier:
                raise ValueError("Invalid record")
            _key(_provider(row["provider"]), row["api_key"])
            if (not re.fullmatch(r"[a-f0-9]{64}", row["fingerprint"])
                    or not _number(row["created_at"]) or not isinstance(row["verification"], dict)):
                raise ValueError("Invalid record")
        except (KeyError, TypeError, ValueError):
            raise vault_error() from None
        return row

    def _audit(self, state, action, provider, identifier=None, code=None):
        event = {"at": self.clock(), "action": action, "provider": provider,
                 "credential_id": identifier, "revision": state["revision"] + 1}
        if code in ERROR_CODES:
            event["code"] = code
        state["audit"].append(event)

    def stage(self, provider, api_key):
        provider = _provider(provider)
        api_key = _key(provider, api_key)
        def operation(state):
            fingerprint = _digest(state, "credential:" + provider, api_key)
            for identifier in state["records"]:
                row = self._record(state, identifier)
                if row["provider"] == provider and hmac.compare_digest(row["fingerprint"], fingerprint):
                    self._audit(state, "stage_existing", provider, identifier)
                    return identifier
            if len(state["records"]) >= MAX_CREDENTIALS:
                raise vault_error("pool_storage_capacity")
            identifier = secrets.token_hex(16)
            state["records"][identifier] = {"credential_id": identifier, "provider": provider,
                "api_key": api_key, "fingerprint": fingerprint, "created_at": self.clock(),
                "account_ref": None, "verification": _verification(), "admission": None}
            self._audit(state, "stage", provider, identifier)
            return identifier
        identifier = self.vault.mutate(operation)
        return {"credential_id": identifier, "pool": self.status()}

    def validate(self, credential_id):
        state, pending = self.vault.read()
        if pending:
            raise vault_error("pool_recovery_required")
        row = self._record(state, credential_id)
        now = self.clock()
        previous = row["verification"].get("checked_at")
        if _number(previous) and 0 <= now - previous < CHECK_COOLDOWN:
            error = fail("pool_check_rate_limited", 429)
            error.retry_after = CHECK_COOLDOWN
            raise error
        account_ref = None
        if row["provider"] not in LIVE_PROVIDERS:
            verification = _verification("balance_unconfirmed", now)
        else:
            try:
                raw = self.usage_probe(row["provider"], row["api_key"])
                verification, account_ref = _parse_usage(row["provider"], raw, now, state)
            except Exception:
                verification = _verification("provider_unavailable", now)
        def operation(current):
            current_row = self._record(current, credential_id)
            # Do not let a slower probe overwrite a newer observation.
            last = current_row["verification"].get("checked_at")
            if _number(last) and last > now:
                raise fail("pool_check_required", 409)
            current_row.update(verification=verification, account_ref=account_ref)
            self._audit(current, "validate", row["provider"], credential_id, verification["code"])
        self.vault.mutate(operation)
        return {"credential_id": credential_id, "pool": self.status()}

    def _admission(self, row, revision):
        provider = row["provider"]
        if provider in LIVE_PROVIDERS:
            verification = row["verification"]
            checked = verification.get("checked_at")
            if (not _number(checked) or not 0 <= self.clock() - checked <= CHECK_TTL
                    or verification.get("free_verified") is not True
                    or not _integer(verification.get("guard_remaining")) or verification["guard_remaining"] < 1):
                raise fail("pool_check_required", 409)
            return None
        candidate = {"provider": provider, "credential_id": row["credential_id"],
                     "account_ref": row["account_ref"], "verification": deepcopy(row["verification"]), "revision": revision}
        try:
            admission = self.activation_policy(candidate) if self.activation_policy else None
            return self._check_grant(admission, row)
        except Exception:
            raise fail("pool_grant_required", 409) from None

    def _check_grant(self, admission, row):
        if (not isinstance(admission, dict) or admission.get("approved") is not True
                or admission.get("provider") != row["provider"]
                or admission.get("credential_id") != row["credential_id"]
                or any(not isinstance(admission.get(name), str) or not IDENTIFIER.fullmatch(admission[name])
                       for name in ("account_ref", "grant_id"))
                or not _integer(admission.get("remaining")) or admission["remaining"] < 1
                or not _number(admission.get("expires_at")) or admission["expires_at"] <= self.clock()
                or not _number(admission.get("cash_spend_limit_usd")) or admission["cash_spend_limit_usd"] != 0
                or not _number(admission.get("max_unit_cost_usd"))
                or admission["max_unit_cost_usd"] != (0.004 if row["provider"] == "exa" else 0)):
            raise fail("pool_grant_required", 409)
        fields = ("approved", "provider", "credential_id", "account_ref", "grant_id", "remaining",
                  "expires_at", "cash_spend_limit_usd", "max_unit_cost_usd")
        return {name: admission[name] for name in fields}

    def activate(self, credential_id):
        # External policy is read outside the storage lock. The immutable key
        # and its observation are compared again under lock before publication.
        state, pending = self.vault.read()
        if pending:
            raise vault_error("pool_recovery_required")
        row = self._record(state, credential_id)
        admission = self._admission(row, state["revision"])
        def operation(current):
            candidate = self._record(current, credential_id)
            if candidate != row:
                raise fail("pool_check_required", 409)
            if candidate["provider"] in LIVE_PROVIDERS:
                self._admission(candidate, current["revision"])
            else:
                self._check_grant(admission, candidate)
            candidate["admission"] = admission
            current["active"][candidate["provider"]] = credential_id
            self._audit(current, "activate", candidate["provider"], credential_id)
        self.vault.mutate(operation)
        return {"credential_id": credential_id, "pool": self.status()}

    def disable(self, provider):
        provider = _provider(provider)
        def operation(state):
            identifier = state["active"].pop(provider, None)
            self._audit(state, "disable", provider, identifier)
        self.vault.mutate(operation)
        return {"pool": self.status()}

    def resolve_credential(self, credential_id):
        """Internal only; pinned ids keep working through disable/rotation."""
        state, _ = self.vault.read()
        return self._record(state, credential_id)["api_key"]

    def runtime_snapshot(self):
        state, pending = self.vault.read()
        if pending:
            raise vault_error("pool_recovery_required")
        active = {}
        for provider, identifier in state["active"].items():
            _provider(provider)
            row = self._record(state, identifier)
            if row["provider"] != provider:
                raise vault_error()
            # This is configuration, not permission to spend. The runtime must
            # recheck current quota/grant admission independently for every job.
            # One stale provider must not suppress other fallback candidates.
            account_ref = row["account_ref"] or (row["admission"]["account_ref"] if row["admission"] else None)
            active[provider] = {"credential_id": identifier, "account_ref": account_ref,
                                "verification": deepcopy(row["verification"]), "admission": deepcopy(row["admission"])}
        return {"revision": state["revision"], "active": active}

    def _metrics(self):
        try:
            value = self.runtime_status() if self.runtime_status else {}
            if not isinstance(value, dict):
                return {}, False
            json.dumps(value, allow_nan=False)
            return value, self.runtime_status is not None
        except Exception:
            return {}, False

    def _public_row(self, state, row, now):
        verification = row["verification"]
        code = verification.get("code")
        checked = verification.get("checked_at")
        checked = checked if _number(checked) else None
        fresh = checked is not None and 0 <= now - checked <= CHECK_TTL
        result = {"credential_id": row["credential_id"], "provider": row["provider"],
                  "fingerprint": row["fingerprint"][:12], "last4": row["api_key"][-4:],
                  "created_at": row["created_at"], "active": state["active"].get(row["provider"]) == row["credential_id"],
                  "checked_at": checked, "fresh": fresh,
                  "code": code if code in ERROR_CODES else "quota_unconfirmed",
                  "balance_source": "provider_live" if verification.get("balance_source") == "provider_live" else "unconfirmed",
                  "provider_remaining": None, "guard_remaining": None, "plan_limit": None,
                  "free_verified": verification.get("free_verified") is True,
                  "grant": None}
        for name in ("provider_remaining", "guard_remaining", "plan_limit"):
            if _integer(verification.get(name)):
                result[name] = verification[name]
        admission = row.get("admission")
        if isinstance(admission, dict):
            result["grant"] = {"approved": admission.get("approved") is True,
                "remaining": admission["remaining"] if _integer(admission.get("remaining")) else None,
                "expires_at": admission["expires_at"] if _number(admission.get("expires_at")) else None,
                "cash_spend_limit_usd": 0, "max_unit_cost_usd": 0.004 if row["provider"] == "exa" else 0}
        return result

    def status(self):
        state, pending = self.vault.read()
        now = self.clock()
        metrics, connected = self._metrics()
        rows = [self._public_row(state, self._record(state, identifier), now) for identifier in state["records"]]
        providers = []
        raw_providers = metrics.get("providers", {})
        if not isinstance(raw_providers, dict):
            raw_providers = {}
        for provider in PROVIDERS:
            local = raw_providers.get(provider, {})
            if not isinstance(local, dict):
                local = {}
            provider_rows = [row for row in rows if row["provider"] == provider]
            active = next((row for row in provider_rows if row["active"]), None)
            observed = active or max(provider_rows, key=lambda row: row["checked_at"] or 0, default=None)
            health = "disabled" if not active else "unknown"
            if pending:
                health = "recovery_required"
            elif active and provider in LIVE_PROVIDERS:
                health = ("stale" if not active["fresh"] else "blocked" if not active["free_verified"]
                          else "exhausted" if active["guard_remaining"] == 0 else "healthy")
            elif active and active["grant"]:
                # Activation evidence is historical. Only current runtime
                # accounting can establish the health of a bounded grant.
                expiry = local.get("grant_expires_at")
                if local.get("grant_approved") is False:
                    health = "blocked"
                elif (local.get("grant_remaining") == 0
                      or _number(expiry) and expiry <= now):
                    health = "exhausted"
                elif (connected and local.get("grant_approved") is True
                      and _integer(local.get("grant_remaining"))
                      and _number(expiry) and expiry > now):
                    health = "healthy"
                else:
                    health = "unknown"
            if active and not pending and (local.get("local_remaining") == 0
                    or _integer(metrics.get("monthly_used")) and metrics["monthly_used"] >= MONTHLY_LIMIT):
                health = "exhausted"
            item = {"provider": provider, "active_credential_id": active["credential_id"] if active else None,
                    "health": health, "checked_at": observed["checked_at"] if observed else None,
                    "provider_remaining": observed["provider_remaining"] if observed else None,
                    "balance_source": observed["balance_source"] if observed else "unconfirmed",
                    "local_used": None, "local_remaining": None, "grant_remaining": None,
                    "grant_expires_at": None, "grant_approved": local.get("grant_approved") is True}
            for name in ("local_used", "local_remaining", "grant_remaining"):
                if _integer(local.get(name)):
                    item[name] = local[name]
            if _number(local.get("grant_expires_at")):
                item["grant_expires_at"] = local["grant_expires_at"]
            remaining = active["guard_remaining"] if active else None
            item["low_quota"] = (remaining is not None and remaining <= 10
                                 or item["local_remaining"] is not None and item["local_remaining"] <= 10
                                 or item["grant_remaining"] is not None and item["grant_remaining"] <= 10)
            providers.append(item)
        return {"version": 1, "revision": state["revision"], "recovery_required": pending,
                "runtime_connected": connected, "monthly_limit": MONTHLY_LIMIT,
                "monthly_used": metrics.get("monthly_used") if _integer(metrics.get("monthly_used")) else None,
                "providers": providers, "credentials": rows,
                "audit_count": len(state["audit"])}


def route(handler, method, path, body, *, authorize=None):
    """Default-deny adapter; main supplies its independent dashboard guard."""
    if not path.startswith("/api/dashboard/pool"):
        return None
    if authorize is None:
        raise fail("pool_admin_required", 401)
    authorize(handler, method)  # Must precede every access to vault or runtime.
    try:
        if method == "GET" and path == "/api/dashboard/pool":
            return handler.server.beta.pool_admin.status()
        if method != "POST":
            raise fail("pool_method_not_allowed", 405)
        expected = {"stage": {"provider", "api_key"}, "validate": {"credential_id"},
                    "activate": {"credential_id"}, "disable": {"provider"}}
        action = path.removeprefix("/api/dashboard/pool/")
        if action not in expected:
            raise fail("pool_not_found", 404)
        if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise fail("pool_invalid_input", 415)
        length = handler.headers.get("Content-Length", "0")
        if not re.fullmatch(r"[0-9]{1,8}", length) or int(length) > MAX_BODY_BYTES:
            raise fail("pool_body_too_large", 413)
        if not isinstance(body, dict) or set(body) != expected[action]:
            raise fail("pool_invalid_input")
        if len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode()) > MAX_BODY_BYTES:
            raise fail("pool_body_too_large", 413)
        service = handler.server.beta.pool_admin
        if action == "stage":
            return service.stage(body["provider"], body["api_key"])
        if action == "disable":
            return service.disable(body["provider"])
        return getattr(service, action)(body["credential_id"])
    except AccessError:
        raise
    except Exception:
        raise fail("pool_unavailable", 503) from None
