"""Private integration of the credential vault with durable search jobs.

Importing this module performs no I/O. Bootstrap observations configure the vault;
only the native search-time quota/grant checks authorize spending.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from plagiarism.access import AccessError
from plagiarism.search_pool import PoolBudget, checked_grant, fingerprint
from plagiarism.search_pool_admin import PoolAdmin, PROVIDERS, _parse_usage
from plagiarism.web_search import WebSearchError


RUNTIME_PROVIDER = {"serpapi": "serpapi-yandex", "tavily": "tavily", "serper": "serper", "exa": "exa"}
PIN_LIMIT = 4096
BOOTSTRAP_LIMIT = 160_000


def unavailable(code="pool_storage_unavailable"):
    return AccessError("Пул поиска недоступен. Требуется проверка конфигурации сервера.", code, 503)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identifier(value, size):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{" + str(size) + r"}", value) is not None


def validate_bootstrap(packet, keys, now=None):
    """Pure predeploy validation; does not read credentials, state or providers."""
    try:
        if (not isinstance(packet, dict) or set(packet) != {"version", "packet_id", "captured_at", "proofs", "credential_fingerprints"}
                or type(packet["version"]) is not int or packet["version"] != 1
                or not _identifier(packet["packet_id"], 32) or not _number(packet["captured_at"])
                or not isinstance(keys, dict) or set(keys) != set(PROVIDERS)
                or not isinstance(packet["proofs"], dict) or set(packet["proofs"]) != {"serpapi", "tavily"}
                or not isinstance(packet["credential_fingerprints"], dict)
                or set(packet["credential_fingerprints"]) != set(PROVIDERS)
                or len(_json(packet).encode()) > BOOTSTRAP_LIMIT):
            raise ValueError("Invalid packet")
        now = time.time() if now is None else now
        if not _number(now) or not 0 <= now - packet["captured_at"] <= 300:
            raise ValueError("Stale capture")
        for provider in PROVIDERS:
            key = keys[provider]
            if (not isinstance(key, str) or not 16 <= len(key) <= 1024
                    or any(not 33 <= ord(c) <= 126 for c in key)
                    or provider == "tavily" and not key.startswith("tvly-")
                    or packet["credential_fingerprints"][provider] != fingerprint(key)):
                raise ValueError("Fingerprint mismatch")
        for provider, proof in packet["proofs"].items():
            if (not isinstance(proof, dict) or set(proof) != {"key_fingerprint", "checked_at", "usage"}
                    or proof["key_fingerprint"] != fingerprint(keys[provider])
                    or not _number(proof["checked_at"])
                    or not 0 <= now - proof["checked_at"] <= 300
                    or proof["checked_at"] > packet["captured_at"]):
                raise ValueError("Invalid observation")
            usage = proof["usage"]
            if provider == "serpapi":
                fields = {"account_status", "plan_name", "plan_monthly_price", "searches_per_month",
                          "plan_searches_left", "extra_credits", "total_searches_left", "this_month_usage"}
                if not isinstance(usage, dict) or not fields <= set(usage) <= fields | {"account_id"}:
                    raise ValueError("Only redacted account counters are permitted")
            elif (not isinstance(usage, dict) or set(usage) != {"account", "key"}
                  or not isinstance(usage["account"], dict)
                  or set(usage["account"]) != {"current_plan", "plan_usage", "plan_limit", "paygo_usage", "paygo_limit"}
                  or not isinstance(usage["key"], dict) or set(usage["key"]) != {"usage", "limit"}):
                raise ValueError("Only redacted account/key counters are permitted")
            verification, _ = _parse_usage(provider, proof["usage"], proof["checked_at"], {"salt": "0" * 64})
            if not verification["free_verified"] or verification.get("guard_remaining", 0) < 1:
                raise ValueError("Unconfirmed included allowance")
        encoded = _json(packet)
        if any(key in encoded for key in keys.values()):
            raise ValueError("Reflected credential")
        return deepcopy(packet)
    except Exception:
        raise unavailable("pool_unconfigured") from None


class _RuntimeAdmin(PoolAdmin):
    """Apply the integration recovery barrier to every vault configuration write."""

    def __init__(self, runtime, *args, **kwargs):
        self.runtime = runtime
        super().__init__(*args, **kwargs)

    def stage(self, provider, api_key):
        self.runtime._configuration_write()
        return super().stage(provider, api_key)

    def validate(self, credential_id):
        self.runtime._configuration_write()
        return super().validate(credential_id)

    def activate(self, credential_id):
        self.runtime._configuration_write(activation=True)
        return super().activate(credential_id)

    def disable(self, provider):
        self.runtime._configuration_write()
        return super().disable(provider)


class PoolRuntime:
    def __init__(self, beta, *, monthly_limit=180, grants=None, bootstrap=None, keys=None):
        if type(monthly_limit) is not int or monthly_limit not in (180, 900):
            raise ValueError("Reviewed automatic monthly limit must be 180 or 900")
        if grants is not None and (not isinstance(grants, dict) or set(grants) - {"serper", "exa"}):
            raise unavailable("pool_unconfigured")
        self.beta, self.monthly_limit = beta, monthly_limit
        self.grants = deepcopy(grants or {})
        self.marker = Path(beta.runtime) / "pool-bootstrap.json"
        self.pool_admin = _RuntimeAdmin(self, beta.root, beta.persist, monthly_limit=monthly_limit,
                                    runtime_status=self.metrics, activation_policy=self._activation_policy)
        beta.pool_runtime, beta.pool_admin = self, self.pool_admin
        with self._bootstrap_lock():
            marker = self._marker()
            state, pending = self.pool_admin.vault.read()
            initialized = marker is not None or state["revision"] != 0 or bool(state["records"])
            self._init_pins(initialized)
            if marker is not None:
                # A prior incomplete attempt is operator-reconciled, never reseeded.
                if marker["status"] == "complete" and not (self.pool_admin.vault.root / "vault.json").is_file():
                    raise unavailable()
            elif state["revision"] or state["records"] or pending:
                raise unavailable("pool_recovery_required")
            elif bootstrap is not None:
                self._bootstrap(bootstrap, keys)

    @contextmanager
    def _bootstrap_lock(self):
        descriptor = None
        try:
            descriptor = os.open(self.marker.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
                raise OSError("Unsafe bootstrap lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except OSError:
            raise unavailable() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _marker(self):
        descriptor = None
        try:
            descriptor = os.open(self.marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_size > 4096):
                raise OSError("Unsafe bootstrap marker")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                value = json.loads(stream.read(4097))
            if (not isinstance(value, dict) or set(value) != {"version", "packet_digest", "status"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or not _identifier(value["packet_digest"], 64)
                    or value["status"] not in ("in_progress", "complete")):
                raise ValueError("Invalid bootstrap marker")
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError):
            raise unavailable() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write_marker(self, value):
        temporary = self.marker.with_name(".pool-bootstrap-" + secrets.token_hex(16))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(_json(value).encode()); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.marker)
            directory = os.open(self.marker.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _proofs(self, packet, keys):
        try:
            validate_bootstrap(packet, keys)
            for provider in ("serper", "exa"):
                self._grant(provider, keys[provider], current=True)
            return packet["proofs"]
        except Exception:
            raise unavailable("pool_unconfigured") from None

    def _bootstrap(self, packet, keys):
        self._proofs(packet, keys)
        progress = {"version": 1, "packet_digest": sha256(_json(packet).encode()).hexdigest(), "status": "in_progress"}
        original_probe, original_clock = self.pool_admin.usage_probe, self.pool_admin.clock
        self._bootstrapping = True  # Private constructor work, before serving HTTP.
        try:
            self._write_marker(progress)
            self.beta.persist()  # Must precede the first credential mutation.
            for provider in PROVIDERS:
                self._proofs(packet, keys)  # Real time, including grant expiry.
                proof = packet["proofs"].get(provider)
                self.pool_admin.clock = (lambda observed=proof["checked_at"]: observed) if proof else original_clock
                self.pool_admin.usage_probe = lambda name, key: deepcopy(packet["proofs"][name]["usage"])
                identifier = self.pool_admin.stage(provider, keys[provider])["credential_id"]
                self.pool_admin.validate(identifier)
                self.pool_admin.activate(identifier)
            self._proofs(packet, keys)
            if set(self.pool_admin.runtime_snapshot()["active"]) != set(PROVIDERS):
                raise unavailable("pool_recovery_required")
            self._write_marker({**progress, "status": "complete"})
            self.beta.persist()
        except Exception:
            # Keep the already persisted intent; never erase committed versions.
            try:
                self._write_marker(progress)
            except Exception:
                self._bootstrap_failed = True
            raise unavailable("pool_recovery_required") from None
        finally:
            self._bootstrapping = False
            self.pool_admin.usage_probe, self.pool_admin.clock = original_probe, original_clock

    def _configuration_write(self, *, activation=False):
        if getattr(self, "_bootstrapping", False):
            return
        marker = self._marker()
        if getattr(self, "_bootstrap_failed", False) or marker and marker["status"] != "complete":
            raise unavailable("pool_recovery_required")
        if activation and marker is None:
            raise unavailable("pool_unconfigured")
        if marker is not None and not (self.pool_admin.vault.root / "vault.json").is_file():
            raise unavailable()
        _, pending = self.pool_admin.vault.read()
        if pending:
            raise unavailable("pool_recovery_required")

    def _connect(self, readonly=False):
        return sqlite3.connect(Path(self.beta.database).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=15)

    def _init_pins(self, initialized):
        try:
            with closing(self._connect()) as db, db:
                exists = db.execute("SELECT type FROM sqlite_master WHERE name='pool_job_pins' COLLATE NOCASE").fetchone()
                if exists is None:
                    if initialized:
                        raise unavailable()
                    db.execute("CREATE TABLE pool_job_pins(guest TEXT NOT NULL,request_id TEXT NOT NULL,body_hash TEXT NOT NULL,pin_json TEXT NOT NULL,PRIMARY KEY(guest,request_id))")
                columns = [r[1] for r in db.execute("PRAGMA table_info(pool_job_pins)")]
                if exists is not None and exists[0] != "table" or columns != ["guest", "request_id", "body_hash", "pin_json"]:
                    raise unavailable()
        except (sqlite3.Error, OSError):
            raise unavailable() from None

    @staticmethod
    def _request(guest, request_id, body_hash):
        if not _identifier(guest, 64) or not _identifier(request_id, 32) or not _identifier(body_hash, 64):
            raise AccessError("Некорректный идентификатор задания.", "invalid_request", 400)

    @staticmethod
    def _pin(value):
        try:
            if (not isinstance(value, dict) or set(value) != {"version", "revision", "active", "disabled"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or type(value["revision"]) is not int or value["revision"] < 0
                    or not isinstance(value["active"], dict) or set(value["active"]) - set(PROVIDERS)
                    or any(not _identifier(v, 32) for v in value["active"].values())
                    or not isinstance(value["disabled"], list)
                    or value["disabled"] != [p for p in PROVIDERS if p not in value["active"]]
                    or len(_json(value)) > PIN_LIMIT):
                raise ValueError("Invalid pin")
            return deepcopy(value)
        except (ValueError, TypeError):
            raise unavailable() from None

    def _snapshot(self):
        marker = self._marker()
        if getattr(self, "_bootstrap_failed", False) or marker and marker["status"] != "complete":
            raise unavailable("pool_recovery_required")
        if marker is None:
            raise unavailable("pool_unconfigured")
        if not (self.pool_admin.vault.root / "vault.json").is_file():
            raise unavailable()
        return self.pool_admin.runtime_snapshot()

    def get_pin(self, guest, request_id, body_hash):
        """Read an old binding; the caller must prove a durable job exists."""
        self._request(guest, request_id, body_hash)
        try:
            with closing(self._connect(True)) as db:
                row = db.execute("SELECT body_hash,pin_json FROM pool_job_pins WHERE guest=? AND request_id=?", (guest, request_id)).fetchone()
            if row is None:
                return None
            if row[0] != body_hash:
                raise AccessError("Идентификатор уже использован для другого запроса.", "request_conflict", 409)
            return self._pin(json.loads(row[1]))
        except (sqlite3.Error, OSError, ValueError) as error:
            if isinstance(error, AccessError):
                raise
            raise unavailable() from None

    def pin(self, guest, request_id, body_hash):
        self._request(guest, request_id, body_hash)
        snapshot = self._snapshot()  # Even an orphan binding needs fresh admission.
        value = self._pin({"version": 1, "revision": snapshot["revision"],
                           "active": {p: r["credential_id"] for p, r in snapshot["active"].items()},
                           "disabled": [p for p in PROVIDERS if p not in snapshot["active"]]})
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT body_hash,pin_json FROM pool_job_pins WHERE guest=? AND request_id=?", (guest, request_id)).fetchone()
                if row:
                    if row[0] != body_hash:
                        raise AccessError("Идентификатор уже использован для другого запроса.", "request_conflict", 409)
                    retained = self._pin(json.loads(row[1]))
                    if retained["active"] != value["active"] or retained["disabled"] != value["disabled"]:
                        raise AccessError("Конфигурация поиска изменилась. Используйте новый идентификатор задания.", "request_conflict", 409)
                    value = retained
                else:
                    db.execute("INSERT INTO pool_job_pins(guest,request_id,body_hash,pin_json) VALUES(?,?,?,?)", (guest, request_id, body_hash, _json(value)))
            self.beta.persist()
            return value
        except AccessError:
            raise
        except Exception:
            raise unavailable() from None

    def credentials(self, pin):
        value = self._pin(pin)
        state, _ = self.pool_admin.vault.read()  # Last committed versions survive pending writes.
        result = {}
        for provider, identifier in value["active"].items():
            row = self.pool_admin._record(state, identifier)
            if row["provider"] != provider:
                raise unavailable()
            result[RUNTIME_PROVIDER[provider]] = self.pool_admin.resolve_credential(identifier)
        return result

    def _grant(self, provider, key, *, current):
        values = self.grants.get(provider, [])
        values = values if isinstance(values, list) else [values]
        for value in values:
            if isinstance(value, dict) and value.get("credential_fingerprint") == fingerprint(key):
                return checked_grant(value, provider, key) if current else deepcopy(value)
        raise WebSearchError("Бесплатный лимит сервиса не подтверждён.", "free_quota_unconfirmed")

    def grants_for(self, pin):
        keys = self.credentials(pin)
        # Expiry is checked only when the ordered router reaches this candidate.
        result = {}
        for provider in ("serper", "exa"):
            if provider in keys:
                try:
                    result[provider] = self._grant(provider, keys[provider], current=False)
                except WebSearchError:
                    result[provider] = None
        return result

    def _activation_policy(self, candidate):
        provider, identifier = candidate["provider"], candidate["credential_id"]
        key = self.pool_admin.resolve_credential(identifier)
        policy = self._grant(provider, key, current=True)
        remaining = max(0, policy["cap"] - self._spent("grant:" + provider + ":" + policy["account_ref"] + ":" + policy["grant_id"]))
        return {**{k: policy[k] for k in ("approved", "provider", "account_ref", "grant_id", "expires_at", "cash_spend_limit_usd", "max_unit_cost_usd")},
                "credential_id": identifier, "remaining": remaining}

    def _budget_rows(self):
        path = Path(self.beta.root) / "search-budget.sqlite3"
        if not path.exists():
            return None, []
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                month = datetime.now(timezone.utc).strftime("%Y-%m")
                used = db.execute("SELECT count(*) FROM calls WHERE month=?", (month,)).fetchone()[0]
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pool_calls'").fetchone()
                rows = db.execute("SELECT p.provider,p.resource,c.month FROM pool_calls p LEFT JOIN calls c ON c.id=p.id").fetchall() if exists else []
            return used, rows
        except (OSError, sqlite3.Error):
            raise unavailable() from None

    def _spent(self, resource):
        _, rows = self._budget_rows()
        return sum(r[1] == resource for r in rows)

    def metrics(self):
        used, rows = self._budget_rows()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        remaining = max(0, self.monthly_limit - used) if used is not None else None
        state, _ = self.pool_admin.vault.read()
        providers = {}
        for provider in PROVIDERS:
            item = {"local_used": sum(r[2] == month and (r[0] in ("serpapi", "serpapi-yandex") if provider == "serpapi" else r[0] == provider) for r in rows) if used is not None else None,
                    "local_remaining": remaining, "grant_remaining": None,
                    "grant_expires_at": None, "grant_approved": False}
            identifier = state["active"].get(provider)
            if provider in ("serper", "exa") and identifier:
                try:
                    policy = self._grant(provider, self.pool_admin.resolve_credential(identifier), current=True)
                    resource = "grant:" + provider + ":" + policy["account_ref"] + ":" + policy["grant_id"]
                    item.update(grant_remaining=max(0, policy["cap"] - sum(r[1] == resource for r in rows)),
                                grant_expires_at=policy["expires_at"], grant_approved=True)
                except WebSearchError:
                    pass
            providers[provider] = item
        return {"monthly_limit": self.monthly_limit, "monthly_used": used, "providers": providers}

    def searcher(self, pin, provider, needed):
        from plagiarism.search_router import select_search, confirm_tavily
        from plagiarism.search_serpapi import SerpAPISearch, YandexSerpAPISearch
        from plagiarism.search_tavily import TavilySearch
        value = self._pin(pin)
        keys = self.credentials(value)
        path = Path(self.beta.root) / "search-budget.sqlite3"
        if provider == "auto":
            return select_search(path, needed, limit=self.monthly_limit, persist=self.beta.persist,
                                 keys=keys, disabled=[RUNTIME_PROVIDER[p] for p in value["disabled"]], grants=self.grants_for(value))
        canonical = "serpapi" if provider in ("serpapi", "serpapi-yandex") else provider
        if canonical not in ("serpapi", "tavily") or canonical in value["disabled"]:
            raise WebSearchError("Поисковый сервис не подключён.", "web_not_configured")
        key = keys[RUNTIME_PROVIDER[canonical]]
        budget = PoolBudget(path, provider=provider, resource="live:" + canonical,
                            credential_fingerprint=fingerprint(key), limit=180, persist=self.beta.persist)
        budget.preflight(needed)
        factory = TavilySearch if provider == "tavily" else YandexSerpAPISearch if provider == "serpapi-yandex" else SerpAPISearch
        search = factory(budget, key=key)
        if provider == "tavily":
            confirm_tavily(key, needed)
        else:
            search.confirm_free(needed)
        return search
