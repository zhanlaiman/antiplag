"""Own historical reports: exact server stamps and causal terminal-ACK receipts."""
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
import sqlite3
import time

from plagiarism.access import AccessError
from plagiarism.saas import report_identifier

RECEIPT_KIND = 'report-reuse-receipt'
NAMESPACE = 'istok-web-report-terminal-ack-v1'
REPORT_LIMIT, MATCH_LIMIT, REF_LIMIT = 128, 8, 128
BYTE_LIMIT, VM_LIMIT, DEADLINE = 8 * 1024 * 1024, 500000, .25
FIELDS = ('id', 'version', 'namespace', 'tenant', 'job_id', 'request_fingerprint',
          'report_id', 'text_hmac', 'terminal_digest')


def unavailable():
    error = AccessError('Поиск готового отчёта временно недоступен. Откройте историю или повторите позже.',
                        'report_reuse_unavailable', 503)
    error.retry_after = 5
    return error


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(size) + '}', value) is not None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def text_fingerprint(secret, tenant, text):
    if not _hex(tenant, 64) or not isinstance(text, str): raise ValueError('Invalid reuse input')
    return hmac.new(secret, _json(['web-report-text-v1', tenant, text]).encode('utf-8'), sha256).hexdigest()


def manifest(secret, tenant, text, job_id, request_fingerprint):
    if not _hex(job_id, 32) or not _hex(request_fingerprint, 64): raise ValueError('Invalid job identity')
    return {'version': 1, 'text_hmac': text_fingerprint(secret, tenant, text),
            'job_id': job_id, 'request_fingerprint': request_fingerprint}


def _stamp(raw):
    if raw is None: return None
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        return None
    if (set(value) != {'version', 'text_hmac', 'job_id', 'request_fingerprint'}
            or not _hex(value['text_hmac'], 64) or not _hex(value['job_id'], 32)
            or not _hex(value['request_fingerprint'], 64)):
        raise unavailable()
    return value


class _Budget:
    def __init__(self):
        self.until = time.monotonic() + DEADLINE
        self.bytes = self.steps = self.refs = self.matches = 0

    def check(self, size=0):
        self.bytes += size
        if (self.bytes > BYTE_LIMIT or self.steps > VM_LIMIT or time.monotonic() >= self.until):
            raise unavailable()

    def progress(self):
        self.steps += 100
        return int(self.steps > VM_LIMIT or time.monotonic() >= self.until)


@contextmanager
def _reader(root, tenant):
    if not _hex(tenant, 64): raise unavailable()
    base = Path(root) / 'tenants'; directory = base / tenant; path = directory / 'istok.sqlite3'
    db = None
    try:
        if any(part.is_symlink() for part in (base, directory, path)): raise unavailable()
        if not path.exists():
            yield None, None
            return
        if not path.is_file(): raise unavailable()
        budget = _Budget()
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)
        db.row_factory = sqlite3.Row
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, BYTE_LIMIT)
        db.set_progress_handler(budget.progress, 100)
        budget.check()
        yield db, budget
    except (sqlite3.Error, OSError, ValueError, TypeError, UnicodeError):
        raise unavailable() from None
    finally:
        if db is not None:
            db.set_progress_handler(None, 0)
            db.close()


def _record(db, budget, kind, identifier):
    size = db.execute('SELECT length(CAST(data AS BLOB)) FROM records WHERE kind=? AND id=?',
                      (kind, identifier)).fetchone()
    if size is None: return None
    budget.check(size[0])
    row = db.execute('SELECT data FROM records WHERE kind=? AND id=?', (kind, identifier)).fetchone()
    return row[0] if row else None


def make_receipt(root, secret, capture):
    """Called only by the post-ACK1 settled-Future observer; never by lookup."""
    tenant, job_id, request_fp, report_id, digest = capture
    with _reader(root, tenant) as (db, budget):
        if db is None: return None
        size = db.execute("SELECT length(CAST(data AS BLOB)) FROM records WHERE kind='report' AND id=?",
                          (report_id,)).fetchone()
        if size is None: return None
        budget.check(size[0])
        row = db.execute("SELECT json_extract(data,'$._report_reuse'),json_extract(data,'$.scope') "
                         "FROM records WHERE kind='report' AND id=?", (report_id,)).fetchone()
        stamp = _stamp(row[0])
        if (not stamp or row[1] != 'web_search' or stamp['job_id'] != job_id
                or stamp['request_fingerprint'] != request_fp or report_identifier(job_id) != report_id):
            return None
    receipt = {'id': 'report-reuse-ack:' + job_id, 'version': 1, 'namespace': NAMESPACE,
               'tenant': tenant, 'job_id': job_id, 'request_fingerprint': request_fp,
               'report_id': report_id, 'text_hmac': stamp['text_hmac'], 'terminal_digest': digest}
    receipt['signature'] = hmac.new(secret, _json(receipt).encode(), sha256).hexdigest()
    return receipt


def _verify(receipt, secret, tenant, identifier, stamp, raw_job):
    if (not isinstance(receipt, dict) or set(receipt) != set(FIELDS) | {'signature', 'created_at'}
            or type(receipt.get('version')) is not int or receipt['version'] != 1
            or not _hex(receipt.get('signature'), 64)):
        raise unavailable()
    signed = {key: receipt[key] for key in FIELDS}
    expected = hmac.new(secret, _json(signed).encode(), sha256).hexdigest()
    bindings = {'namespace': NAMESPACE, 'tenant': tenant, 'job_id': stamp['job_id'],
                'request_fingerprint': stamp['request_fingerprint'], 'report_id': identifier,
                'text_hmac': stamp['text_hmac'], 'id': 'report-reuse-ack:' + stamp['job_id'],
                'terminal_digest': sha256(raw_job.encode('utf-8')).hexdigest()}
    if not hmac.compare_digest(receipt['signature'], expected) or any(receipt[k] != v for k, v in bindings.items()):
        raise unavailable()


def _sources(db, budget, identifier):
    row = db.execute("SELECT json_extract(data,'$.sources_refs'),json_type(data,'$.sources') "
                     "FROM records WHERE kind='report' AND id=?", (identifier,)).fetchone()
    if row is None: raise unavailable()
    if row[0] is None:
        if row[1] != 'array': raise unavailable()
        return
    refs = json.loads(row[0])
    if not isinstance(refs, list): raise unavailable()
    budget.refs += len(refs)
    if budget.refs > REF_LIMIT: raise unavailable()
    for reference in refs:
        if not _hex(reference, 64): raise unavailable()
        size = db.execute('SELECT length(CAST(data AS BLOB)) FROM source_snapshots WHERE hash=?', (reference,)).fetchone()
        if size is None: raise unavailable()
        budget.check(size[0])
        valid = db.execute("SELECT CASE WHEN json_valid(data) THEN json_type(data)='object' "
                           "AND json_type(data,'$.id')='text' AND json_type(data,'$.text')='text' "
                           "AND json_type(data,'$.url')='text' ELSE 0 END FROM source_snapshots WHERE hash=?", (reference,)).fetchone()[0]
        if valid != 1:
            raise unavailable()
        raw = db.execute('SELECT data FROM source_snapshots WHERE hash=?', (reference,)).fetchone()[0]
        if sha256(raw.encode('utf-8')).hexdigest() != reference:
            raise unavailable()


def lookup(root, secret, tenant, text):
    expected = text_fingerprint(secret, tenant, text)
    with _reader(root, tenant) as (db, budget):
        if db is None: return None
        rows = db.execute("SELECT r.id,r.created_at,length(CAST(r.data AS BLOB)) FROM records r "
                          "WHERE r.kind='report' AND NOT EXISTS(SELECT 1 FROM trash WHERE kind='report' AND id=r.id) "
                          "ORDER BY r.created_at DESC,r.id DESC LIMIT ?", (REPORT_LIMIT + 1,))
        for index, row in enumerate(rows):
            if index >= REPORT_LIMIT: raise unavailable()
            identifier, created_at, size = row
            budget.check(size)
            metadata = db.execute("SELECT json_extract(data,'$._report_reuse'),json_extract(data,'$.scope'),"
                "json_extract(data,'$.web.provider'),json_extract(data,'$.web.query_strategy'),"
                "json_extract(data,'$.web.status'),json_extract(data,'$.web.queries_planned'),"
                "json_extract(data,'$.web.queries_completed'),json_extract(data,'$.web.automatic_search.consent_version'),"
                "json_extract(data,'$.web.excluded_source_host') FROM records WHERE kind='report' AND id=?", (identifier,)).fetchone()
            if metadata is None: continue
            stamp = _stamp(metadata[0])
            if not stamp or not hmac.compare_digest(stamp['text_hmac'], expected): continue
            budget.matches += 1
            if budget.matches > MATCH_LIMIT: raise unavailable()
            if not _hex(identifier, 32) or report_identifier(stamp['job_id']) != identifier: raise unavailable()
            raw_job = _record(db, budget, 'model-job', stamp['job_id'])
            if raw_job is None: continue
            job = json.loads(raw_job)
            if not isinstance(job, dict): raise unavailable()
            result = job.get('result')
            if not isinstance(result, dict) or not isinstance(result.get('report'), dict): continue
            if (job.get('status') != 'complete' or job.get('path') != '/api/web-check'
                    or job.get('request_fingerprint') != stamp['request_fingerprint']
                    or result['report'].get('id') != identifier): continue
            raw_receipt = _record(db, budget, RECEIPT_KIND, 'report-reuse-ack:' + stamp['job_id'])
            if raw_receipt is None: continue
            _verify(json.loads(raw_receipt), secret, tenant, identifier, stamp, raw_job)
            if metadata[1] != 'web_search' or not isinstance(created_at, str): raise unavailable()
            if datetime.fromisoformat(created_at.replace('Z', '+00:00')).tzinfo is None: raise unavailable()
            _sources(db, budget, identifier)
            budget.check()
            result = {'report_id': identifier, 'created_at': created_at,
                      'provider': metadata[2] if metadata[2] in {'serpapi', 'serpapi-yandex', 'serper', 'tavily', 'exa'} else None,
                      'query_strategy': metadata[3] if isinstance(metadata[3], str) and len(metadata[3]) <= 128 else None,
                      'coverage_status': metadata[4] if metadata[4] in {'complete', 'partial'} else 'unknown'}
            for name, value in (('queries_planned', metadata[5]), ('queries_completed', metadata[6])):
                result[name] = value if type(value) is int and value >= 0 else None
            if isinstance(metadata[7], str) and len(metadata[7]) <= 64: result['consent_version'] = metadata[7]
            if isinstance(metadata[8], str) and re.fullmatch(r'[A-Za-z0-9.:-]{1,253}', metadata[8]):
                result['excluded_source_host'] = metadata[8]
            return result
        budget.check()
        return None
