"""Read-only account-space inventory. Does not read texts, credentials or email hashes."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from plagiarism.access import AccessError


PAGE_SIZE = 50
SAFE_CODES = frozenset({"job_interrupted", "job_storage_error", "job_submission_failed", "model_error",
                        "invalid_request", "record_not_found", "account_blocked", "free_quota_rate_limited",
                        "web_not_configured", "web_search_failed", "web_search_timeout"})


def valid_account_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise AccessError("Некорректный аккаунт.", "dashboard_invalid_account", 400)
    return value


def _read(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise OSError("symlink")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    return closing(db)


def _has(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


class AccountInventory:
    def __init__(self, beta, account_ledger=None):
        self.beta, self.account_ledger = beta, account_ledger

    def identities(self):
        accounts = {}
        try:
            with _read(self.beta.database) as db:
                for row in db.execute("SELECT id,month FROM guests"):
                    if re.fullmatch(r"[a-f0-9]{64}", row[0]):
                        month = row[1] if isinstance(row[1], str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}", row[1]) else None
                        accounts[row[0]] = {"id": row[0], "kind": "guest", "source": "beta", "first_seen_month": month}
                for row in db.execute("SELECT DISTINCT id FROM active_guests"):
                    if re.fullmatch(r"[a-f0-9]{64}", row[0]):
                        accounts.setdefault(row[0], {"id": row[0], "kind": "guest", "source": "beta", "first_seen_month": None})
                if _has(db, "owner_account"):
                    row = db.execute("SELECT guest FROM owner_account WHERE id=1").fetchone()
                    if row and re.fullmatch(r"[a-f0-9]{64}", row[0]):
                        accounts.setdefault(row[0], {"id": row[0], "source": "beta", "first_seen_month": None})["kind"] = "owner"
            for directory in (self.beta.root / "tenants").glob("*"):
                if re.fullmatch(r"[a-f0-9]{64}", directory.name):
                    accounts.setdefault(directory.name, {"id": directory.name, "kind": "guest", "source": "beta", "first_seen_month": None})
        except (OSError, sqlite3.Error):
            raise AccessError("Список аккаунтов временно недоступен.", "dashboard_inventory_unavailable", 503) from None
        registered = "not_connected"
        if self.account_ledger is not None:
            try:
                with _read(self.account_ledger.path) as db:
                    for row in db.execute("SELECT id,created_at FROM accounts"):
                        if re.fullmatch(r"[a-f0-9]{64}", row[0]):
                            accounts.setdefault(row[0], {"id": row[0], "kind": "registered", "source": "saas_ledger", "registered_at": row[1], "first_seen_month": None})
                registered = "connected"
            except (OSError, sqlite3.Error):
                registered = "unavailable"
        return accounts, registered

    def metadata(self, account_id):
        day = datetime.now(timezone.utc).date().isoformat()
        path = self.beta.root / "tenants" / account_id / "istok.sqlite3"
        result = {"available": True, "reports_total": 0, "reports_active": 0,
                  "characters_total": 0, "characters_today": 0, "unknown_character_reports": 0,
                  "errors_total": 0, "errors_today": 0, "recent_errors": [], "last_record_at": None}
        if not path.exists() and not path.is_symlink():
            return result
        try:
            with _read(path) as db:
                trash = _has(db, "trash")
                active = "NOT EXISTS(SELECT 1 FROM trash WHERE kind='report' AND trash.id=records.id)" if trash else "1"
                # JSON scalar projections run inside SQLite; no report/document data is hydrated.
                rows = db.execute("SELECT count(*) AS total,coalesce(sum(" + active + "),0) AS active,"
                    "coalesce(sum(chars),0) AS chars,coalesce(sum(CASE WHEN substr(created_at,1,10)=? THEN chars ELSE 0 END),0) AS today,"
                    "coalesce(sum(chars IS NULL),0) AS unknown FROM (SELECT id,created_at,"
                    "CASE WHEN json_valid(data) THEN CASE WHEN json_type(data,'$.stats.total_characters')='integer' "
                    "AND json_extract(data,'$.stats.total_characters')>=0 THEN json_extract(data,'$.stats.total_characters') END END AS chars "
                    "FROM records WHERE kind='report') AS records", (day,)).fetchone()
                result.update(reports_total=rows["total"], reports_active=rows["active"], characters_total=rows["chars"],
                              characters_today=rows["today"], unknown_character_reports=rows["unknown"])
                result["last_record_at"] = _timestamp(db.execute("SELECT max(created_at) FROM records").fetchone()[0])
                # Only allowlisted codes are emitted. Error messages/details may contain private text.
                failure = "kind='model-job' AND CASE WHEN json_valid(data) THEN json_extract(data,'$.status')='failed' ELSE 0 END"
                errors = db.execute("SELECT count(*),coalesce(sum(substr(created_at,1,10)=?),0) FROM records WHERE " + failure, (day,)).fetchone()
                result.update(errors_total=errors[0], errors_today=errors[1])
                for row in db.execute("SELECT created_at,json_extract(data,'$.code') AS code FROM records WHERE " + failure + " ORDER BY created_at DESC LIMIT 5"):
                    result["recent_errors"].append({"at": _timestamp(row[0]), "code": row[1] if row[1] in SAFE_CODES else "job_failed"})
        except (OSError, sqlite3.Error):
            return {"available": False}
        return result

    def page(self, controls, offset=0):
        identities, registered = self.identities()
        for identifier in controls:
            identities.setdefault(identifier, {"id": identifier, "kind": "guest", "source": "admin_control", "first_seen_month": None})
        ids = sorted(identities)
        page = []
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            with _read(self.beta.database) as db:
                for identifier in ids[offset:offset + PAGE_SIZE]:
                    item = dict(identities[identifier]); item["metrics"] = self.metadata(identifier)
                    counts = db.execute("SELECT count(*),coalesce(sum(day=?),0),max(day) FROM checks WHERE guest=? AND NOT EXISTS(SELECT 1 FROM unstarted_checks u WHERE u.guest=checks.guest AND u.id=checks.id)", (today, identifier)).fetchone()
                    last_day = db.execute("SELECT max(day) FROM rates WHERE guest=?", (identifier,)).fetchone()[0]
                    days = [value for value in (counts[2], last_day) if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)]
                    last_record = item["metrics"].get("last_record_at")
                    if last_record and (not days or last_record[:10] >= max(days)):
                        item["last_usage"] = {"at": last_record, "precision": "timestamp"}
                    else:
                        item["last_usage"] = {"at": max(days), "precision": "day"} if days else None
                    item["web_checks_admitted"] = {"total": counts[0], "today": counts[1]}
                    control = controls.get(identifier)
                    item["blocked"] = bool(control and control["blocked"])
                    item["block_reason"] = control["reason"] if control else None
                    item["control_changed_at"] = control["changed_at"] if control else None
                    page.append(item)
        except (OSError, sqlite3.Error):
            raise AccessError("Статистика временно недоступна.", "dashboard_inventory_unavailable", 503) from None
        coordinator = self.beta.tenants.coordinator
        with coordinator.lock:
            queue = {"active": coordinator.active, "capacity": coordinator.max_active, "scope": "shared_library_and_web"}
        return {"accounts": page, "total": len(ids), "offset": offset, "limit": PAGE_SIZE,
                "has_more": offset + PAGE_SIZE < len(ids), "queue": queue, "day_utc": today,
                "registration": {"source": "existing_saas_email_otp", "ledger": registered, "beta_connected": False},
                "payments": {"configured": False, "source": None, "purchases": None},
                "as_of": datetime.now(timezone.utc).isoformat()}
