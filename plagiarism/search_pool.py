"""Atomic shared-call and free-grant accounting; uncertain calls stay consumed.

The existing calls table remains authoritative for the site's monthly ceiling.
Grant counters are lifetime counters and do not reset when a key/month changes.
"""
from datetime import timezone
from hashlib import sha256
import json
import math
import re
import time

from plagiarism.web_search import SearchBudget, WebSearchError
from plagiarism.search_provider_policy import FreeQuotaExhausted


def fingerprint(key):
    return sha256(key.encode('utf-8')).hexdigest()


def checked_grant(value, provider, key, now=None):
    now = time.time() if now is None else now
    if not isinstance(value, dict):
        raise WebSearchError('Бесплатный лимит сервиса не подтверждён.', 'free_quota_unconfirmed')
    expiry = value.get('expires_at')
    maximum = 0.004 if provider == 'exa' else 0
    if (value.get('provider') != provider or value.get('credential_fingerprint') != fingerprint(key)
            or value.get('approved') is not True or value.get('cash_spend_limit_usd') != 0
            or type(value.get('cash_spend_limit_usd')) not in (int, float)
            or type(value.get('max_unit_cost_usd')) not in (int, float)
            or value.get('max_unit_cost_usd') != maximum
            or type(value.get('cap')) is not int or not 1 <= value['cap'] <= 2500
            or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= now
            or any(not isinstance(value.get(k), str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', value[k])
                   for k in ('grant_id', 'account_ref'))
            or not isinstance(value.get('proof_sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', value['proof_sha256'])):
        raise WebSearchError('Бесплатный лимит сервиса не подтверждён.', 'free_quota_unconfirmed')
    return dict(value)


class PoolBudget(SearchBudget):
    def __init__(self, path, *, provider, resource, credential_fingerprint, grant=None,
                 limit=180, persist=lambda: None, now=None):
        super().__init__(path, limit, now)
        self.provider, self.resource = provider, resource
        self.credential_fingerprint = credential_fingerprint
        self.grant = dict(grant) if grant is not None else None
        self.persist = persist
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS pool_calls('
                'id TEXT PRIMARY KEY,provider TEXT NOT NULL,resource TEXT NOT NULL,fingerprint TEXT NOT NULL);'
                'CREATE INDEX IF NOT EXISTS pool_calls_resource ON pool_calls(resource);'
                'CREATE TABLE IF NOT EXISTS pool_grants('
                'resource TEXT PRIMARY KEY,policy TEXT NOT NULL);')
            if grant is not None:
                db.execute('BEGIN IMMEDIATE')
                # Key fingerprint is a binding, never the identity of a new grant.
                policy = json.dumps({k: grant[k] for k in ('grant_id', 'account_ref', 'provider', 'cap',
                    'expires_at', 'cash_spend_limit_usd', 'max_unit_cost_usd', 'proof_sha256')}, sort_keys=True)
                row = db.execute('SELECT policy FROM pool_grants WHERE resource=?', (resource,)).fetchone()
                if row and row[0] != policy:
                    raise WebSearchError('Политика бесплатного лимита изменилась.', 'free_quota_unconfirmed')
                db.execute('INSERT OR IGNORE INTO pool_grants VALUES(?,?)', (resource, policy))

    def _check(self, db, needed):
        month = self.now().astimezone(timezone.utc).strftime('%Y-%m')
        used = db.execute('SELECT count(*) FROM calls WHERE month=?', (month,)).fetchone()[0]
        if used + needed > self.limit:
            raise WebSearchError('Достигнут общий месячный лимит поискового API.', 'search_budget_exhausted')
        if self.grant:
            if self.grant['expires_at'] <= self.now().timestamp():
                raise WebSearchError('Бесплатный лимит сервиса нужно подтвердить заново.', 'free_quota_unconfirmed')
            spent = db.execute('SELECT count(*) FROM pool_calls WHERE resource=?', (self.resource,)).fetchone()[0]
            if spent + needed > self.grant['cap']:
                raise FreeQuotaExhausted()
        return month

    def preflight(self, needed):
        if type(needed) is not int or not 1 <= needed <= 6:
            raise ValueError('Invalid planned search count')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check(db, needed)

    def reserve(self, identifier):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM calls WHERE id=?', (identifier,)).fetchone():
                raise WebSearchError('Этот поисковый запрос уже отправлялся.', 'search_already_attempted')
            month = self._check(db, 1)
            db.execute("INSERT INTO calls VALUES(?,?,'started')", (identifier, month))
            db.execute('INSERT INTO pool_calls VALUES(?,?,?,?)',
                       (identifier, self.provider, self.resource, self.credential_fingerprint))
        # Failure here leaves a consumed row, and the adapter sends no HTTP call.
        self.persist()

    def finish(self, identifier):
        super().finish(identifier)
        self.persist()
