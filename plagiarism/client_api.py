"""Owner-issued, scoped and revocable bearer keys. No shared cookie bypass."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import re
import secrets
import sqlite3
import time

from plagiarism.access import AccessError

SCOPES = frozenset({'reports:read', 'sources:read', 'sources:write', 'checks:write', 'quota:read'})
TOKEN = re.compile(r'istok_v1_([a-f0-9]{32})\.([A-Za-z0-9_-]{43})')


class APIKeys:
    def __init__(self, database, persist=lambda: None, now=time.time):
        self.database = database
        self.persist = persist
        self.now = now
        with closing(self.connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,guest TEXT NOT NULL,label TEXT NOT NULL,
                digest TEXT NOT NULL,scopes TEXT NOT NULL,created_at REAL NOT NULL,
                expires_at REAL NOT NULL,revoked_at REAL,last_used_at REAL
                ); CREATE TABLE IF NOT EXISTS api_key_rates (
                id TEXT NOT NULL,minute INTEGER NOT NULL,count INTEGER NOT NULL,
                PRIMARY KEY(id,minute));''')

    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def public(row):
        def date(value):
            return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None
        return {'id': row['id'], 'label': row['label'], 'scopes': json.loads(row['scopes']),
                'created_at': date(row['created_at']), 'expires_at': date(row['expires_at']),
                'revoked_at': date(row['revoked_at']), 'last_used_at': date(row['last_used_at'])}

    def list(self, guest):
        with closing(self.connect()) as db:
            return [self.public(row) for row in db.execute('SELECT * FROM api_keys WHERE guest=? ORDER BY (revoked_at IS NULL AND expires_at>?) DESC,created_at DESC,id LIMIT 30', (guest,self.now()))]

    def create(self, guest, label, scopes, days=30):
        if not isinstance(guest, str) or not re.fullmatch('[a-f0-9]{64}', guest):
            raise ValueError('Некорректное пространство API.')
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise ValueError('Укажите название ключа до 80 знаков.')
        if (not isinstance(scopes, list) or not scopes or len(scopes)>len(SCOPES)
                or any(not isinstance(scope,str) or scope not in SCOPES for scope in scopes)):
            raise ValueError('Выберите разрешения для ключа.')
        if type(days) is not int or not 1 <= days <= 90:
            raise ValueError('Срок ключа — от 1 до 90 дней.')
        now = self.now()
        identifier = secrets.token_hex(16)
        token = 'istok_v1_' + identifier + '.' + secrets.token_urlsafe(32)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM api_keys WHERE guest=? AND revoked_at IS NULL AND expires_at>?', (guest, now)).fetchone()[0] >= 3:
                raise AccessError('Доступно до трёх активных ключей. Отзовите один из них.', 'api_key_limit', 429)
            db.execute('INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,NULL,NULL)',
                       (identifier, guest, label.strip(), sha256(token.encode()).hexdigest(),
                        json.dumps(sorted(set(scopes))), now, now+days*86400))
            public = self.public(db.execute('SELECT * FROM api_keys WHERE id=?', (identifier,)).fetchone())
        self.persist()
        return {'key': public, 'token': token, 'shown_once': True}

    def revoke(self, guest, identifier):
        with closing(self.connect()) as db, db:
            changed = db.execute('UPDATE api_keys SET revoked_at=coalesce(revoked_at,?) WHERE guest=? AND id=?',
                                 (self.now(), guest, identifier)).rowcount
        if not changed:
            raise KeyError(identifier)
        self.persist()
        return {'ok': True}

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or not authorization.startswith('Bearer '):
            raise AccessError('Нужен заголовок Authorization: Bearer <ключ>.', 'api_auth_required', 401)
        token = authorization[7:]
        match = TOKEN.fullmatch(token)
        if not match:
            raise AccessError('Ключ API недействителен или истёк.', 'api_key_invalid', 401)
        now = self.now()
        minute = int(now//60)
        commit_volume = False
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM api_keys WHERE id=?', (match.group(1),)).fetchone()
            expected = row['digest'] if row else '0'*64
            valid = hmac.compare_digest(expected, sha256(token.encode()).hexdigest())
            if not valid or row is None or row['revoked_at'] is not None or row['expires_at'] <= now:
                raise AccessError('Ключ API недействителен или истёк.', 'api_key_invalid', 401)
            count = db.execute('SELECT count FROM api_key_rates WHERE id=? AND minute=?', (row['id'], minute)).fetchone()
            if count and count[0] >= 120:
                error = AccessError('До 120 запросов API в минуту на ключ. Повторите позже.', 'api_rate_limit', 429)
                error.retry_after = max(1, 60-int(now%60))
                raise error
            db.execute('INSERT INTO api_key_rates VALUES (?,?,1) ON CONFLICT(id,minute) DO UPDATE SET count=count+1', (row['id'], minute))
            db.execute('DELETE FROM api_key_rates WHERE minute<?', (minute-1440,))
            if row['last_used_at'] is None or int(row['last_used_at']//60) != minute:
                db.execute('UPDATE api_keys SET last_used_at=? WHERE id=?', (now, row['id']))
                commit_volume = True
            key = {'id': row['id'], 'guest': row['guest'], 'scopes': frozenset(json.loads(row['scopes']))}
        if commit_volume:
            self.persist()
        return key


def require_scope(key, scope):
    if scope not in key['scopes']:
        raise AccessError('У ключа нет разрешения на эту операцию.', 'api_scope_forbidden', 403)
