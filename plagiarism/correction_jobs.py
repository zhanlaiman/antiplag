"""Durable, bounded admissions for optional GPU corrections; no paid fallback."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import sqlite3

from plagiarism.access import AccessError
from plagiarism.ai_proofreading import request_payload
from plagiarism import ai_rewriting

MONTHLY_LIMIT = 10
SITE_DAILY_LIMIT = 3
GUEST_DAILY_LIMIT = 2
REWRITE_APP = 'istok-corrector-rewrite-v1'
_CAPABILITY_FIELDS = {'schema_version', 'app', 'class_name', 'environment', 'deployment_id',
                      'model_digest', 'service_manifest_sha256', 'mode', 'version',
                      'max_characters', 'max_output_characters'}


def _hex(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _rewrite_text(value):
    return (isinstance(value, str) and bool(value.strip()) and 1 <= len(value) <= 1500
            and not any(0xD800 <= ord(c) <= 0xDFFF or (ord(c) < 32 and c not in '\t\r\n')
                        or ord(c) == 127 for c in value))


def _capability_fields(value, model_digest):
    if not isinstance(value, dict) or set(value) != _CAPABILITY_FIELDS:
        return None
    expected = {'schema_version': 1, 'app': REWRITE_APP, 'class_name': 'QwenCorrector',
                'environment': 'main', 'mode': ai_rewriting.MODE, 'version': ai_rewriting.VERSION,
                'max_characters': 1000, 'max_output_characters': 1500}
    if any(type(value.get(k)) is not type(v) or value[k] != v for k, v in expected.items()):
        return None
    if (not all(_hex(value[k]) for k in ('deployment_id', 'model_digest', 'service_manifest_sha256'))
            or value['model_digest'] != model_digest):
        return None
    return dict(value)


def validate_rewrite_capability(value, model_digest):
    """Validate sanitized trusted CPU metadata; never read files or probe SDK."""
    if not isinstance(value, dict) or set(value) != _CAPABILITY_FIELDS | {'qualification_sha256'}:
        return None
    fields = _capability_fields({k: value[k] for k in _CAPABILITY_FIELDS}, model_digest)
    if fields is None or not _hex(value['qualification_sha256']):
        return None
    return {**fields, 'qualification_sha256': value['qualification_sha256']}


def rewrite_capability_json(raw, model_digest):
    if not isinstance(raw, str) or not 0 < len(raw) <= 65536:
        return None
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate capability key')
            result[key] = value
        return result
    def constant(value): raise ValueError('Non-finite capability')
    try:
        return validate_rewrite_capability(json.loads(raw, object_pairs_hook=pairs, parse_constant=constant), model_digest)
    except (ValueError, RecursionError):
        return None


def qualified_rewrite_capability(descriptor, qualification, model_digest):
    """Builder-only metadata binding; caller must verify the actual ref bytes."""
    if not isinstance(descriptor, dict) or set(descriptor) != _CAPABILITY_FIELDS | {'qualification_ref'}:
        return None
    fields = _capability_fields({k: descriptor[k] for k in _CAPABILITY_FIELDS}, model_digest)
    ref = descriptor['qualification_ref']
    if (fields is None or not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}
            or not isinstance(ref['path'], str) or not ref['path'] or not _hex(ref['sha256'])
            or not isinstance(qualification, dict)
            or set(qualification) != {'kind', 'status', 'ready_for_public', 'capability', 'model_smoke', 'independent_review_sha256'}
            or qualification['kind'] != 'qwen-rewrite-qualification-v1' or qualification['status'] != 'PASS'
            or qualification['ready_for_public'] is not True or not _hex(qualification['independent_review_sha256'])):
        return None
    if _capability_fields(qualification['capability'], model_digest) != fields:
        return None
    smoke = qualification['model_smoke']
    expected = {'complete': True, **{k: fields[k] for k in ('deployment_id', 'model_digest', 'mode', 'version')}}
    if (not isinstance(smoke, dict) or set(smoke) != set(expected)
            or any(type(smoke[k]) is not type(v) or smoke[k] != v for k, v in expected.items())):
        return None
    return {**fields, 'qualification_sha256': ref['sha256']}


def correction_request(text, *, mode='grammar', version=None, protected_literals=()):
    """Shared validation/identity; preserve the exact legacy text-only hash."""
    if mode == 'grammar':
        request_payload(text)
        return {'text': text}, sha256(text.encode()).hexdigest()
    if mode != ai_rewriting.MODE or version != ai_rewriting.VERSION:
        raise AccessError('Неизвестный режим или версия Qwen.', 'correction_mode', 400)
    ai_rewriting.request_payload(text, protected_literals)
    fields = {'mode': mode, 'version': version, 'text': text, 'protected_literals': list(protected_literals)}
    fingerprint = sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return fields, fingerprint


def internal_id(guest, request_id):
    if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
        raise ValueError('Нужен сохранённый ID проверки: 32 строчные hex-цифры.')
    return sha256(('ai-correction\0' + guest + '\0' + request_id).encode()).hexdigest()[:32]


def correction_id(identifier):
    return sha256(('correction\0' + identifier).encode()).hexdigest()[:32]


class CorrectionQuota:
    def __init__(self, database, persist=lambda: None, now=lambda: datetime.now(timezone.utc)):
        self.database, self.persist, self.now = database, persist, now
        with closing(self.connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS ai_correction_checks(
                guest TEXT NOT NULL,id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                day TEXT NOT NULL,month TEXT NOT NULL,PRIMARY KEY(guest,id));''')

    def connect(self):
        return sqlite3.connect(self.database, timeout=15)

    def remaining(self, guest):
        now = self.now(); day = now.strftime('%Y-%m-%d'); month = day[:7]
        with closing(self.connect()) as db:
            monthly = db.execute('SELECT count(*) FROM ai_correction_checks WHERE month=?', (month,)).fetchone()[0]
            daily = db.execute('SELECT count(*) FROM ai_correction_checks WHERE day=?', (day,)).fetchone()[0]
            personal = db.execute('SELECT count(*) FROM ai_correction_checks WHERE guest=? AND day=?', (guest, day)).fetchone()[0]
        return {'monthly_remaining': max(0, MONTHLY_LIMIT-monthly),
                'site_daily_remaining': max(0, SITE_DAILY_LIMIT-daily),
                'personal_daily_remaining': max(0, GUEST_DAILY_LIMIT-personal),
                'monthly_limit': MONTHLY_LIMIT, 'site_daily_limit': SITE_DAILY_LIMIT,
                'personal_daily_limit': GUEST_DAILY_LIMIT, 'max_characters': 3000}

    def check_existing(self, guest, identifier, fingerprint):
        with closing(self.connect()) as db:
            old = db.execute('SELECT fingerprint FROM ai_correction_checks WHERE guest=? AND id=?', (guest, identifier)).fetchone()
        if old and old[0] != fingerprint:
            raise AccessError('ID уже использован для другого запроса.', 'request_conflict', 409)
        return bool(old)

    def admit(self, guest, identifier, text, *, mode='grammar', version=None, protected_literals=()):
        internal_id(guest, identifier)
        _, fingerprint = correction_request(text, mode=mode, version=version, protected_literals=protected_literals)
        now = self.now(); day = now.strftime('%Y-%m-%d'); month = day[:7]
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint FROM ai_correction_checks WHERE guest=? AND id=?', (guest, identifier)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise AccessError('ID уже использован для другого текста.', 'request_conflict', 409)
                return False
            if db.execute('SELECT count(*) FROM ai_correction_checks WHERE month=?', (month,)).fetchone()[0] >= MONTHLY_LIMIT:
                raise AccessError('Бесплатный месячный лимит Qwen исчерпан.', 'correction_monthly_limit', 429)
            if db.execute('SELECT count(*) FROM ai_correction_checks WHERE day=?', (day,)).fetchone()[0] >= SITE_DAILY_LIMIT:
                raise AccessError('Бесплатный дневной лимит Qwen для сайта исчерпан.', 'correction_site_daily_limit', 429)
            if db.execute('SELECT count(*) FROM ai_correction_checks WHERE guest=? AND day=?', (guest, day)).fetchone()[0] >= GUEST_DAILY_LIMIT:
                raise AccessError('Ваши две проверки Qwen на сегодня использованы.', 'correction_personal_limit', 429)
            db.execute('INSERT INTO ai_correction_checks VALUES (?,?,?,?,?)', (guest, identifier, fingerprint, day, month))
        self.persist()
        return True

    def release_unqueued(self, guest, identifier):
        """Only after proving no durable reservation and no running/queued job."""
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM ai_correction_checks WHERE guest=? AND id=?', (guest, identifier))
        self.persist()


class ModalCorrector:
    def __init__(self, deployment_id, model_digest, rewrite_capability=None):
        if not re.fullmatch('[a-f0-9]{64}', deployment_id or '') or not re.fullmatch('[a-f0-9]{64}', model_digest or ''):
            raise ValueError('Нужна проверенная версия Qwen.')
        self.deployment_id, self.model_digest = deployment_id, model_digest
        self.rewrite_capability = validate_rewrite_capability(rewrite_capability, model_digest)

    def rewrite_fragment(self, text, protected_literals, store, identifier, progress):
        capability = validate_rewrite_capability(self.rewrite_capability, self.model_digest)
        if capability is None:
            raise AccessError('Переработка Qwen пока недоступна.', 'correction_rewrite_unavailable', 503)
        ai_rewriting.request_payload(text, protected_literals)
        import modal
        service = modal.Cls.from_name(REWRITE_APP, 'QwenCorrector', environment_name='main')()
        progress('Подготавливаем Qwen для ручной переработки', 0, 1)
        call = service.rewrite_fragment.spawn(text, list(protected_literals), capability['deployment_id'])
        store.save('correction-call', {'id': 'correction-call:' + identifier, 'call_id': call.object_id,
                    'deployment_id': capability['deployment_id'], 'mode': ai_rewriting.MODE, 'version': ai_rewriting.VERSION})
        try:
            envelope = call.get(timeout=600)
        except TimeoutError:
            try: call.cancel(terminate_containers=True)
            except Exception: pass
            raise AccessError('Qwen не ответила вовремя. Автоматического повтора нет; исход зарезервирован.',
                              'correction_timeout', 503) from None
        if (not isinstance(envelope, dict) or envelope.get('deployment_id') != capability['deployment_id']
                or envelope.get('model_digest') != self.model_digest):
            raise ValueError('Версия переработки Qwen не совпала.')
        result = envelope.get('result')
        protected_hash = sha256(json.dumps(list(protected_literals), ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        if (not isinstance(result, dict) or result.get('mode') != ai_rewriting.MODE or result.get('version') != ai_rewriting.VERSION
                or result.get('text_sha256') != sha256(text.encode()).hexdigest()
                or result.get('protected_literals_sha256') != protected_hash or result.get('requires_review') is not True
                or not _rewrite_text(result.get('rewritten_text'))):
            raise ValueError('Ответ Qwen не соответствует исходному фрагменту и версии переработки.')
        progress('Сохраняем переработку для проверки автором', 1, 1)
        return result

    def __call__(self, text, store, identifier, progress):
        import modal
        request_payload(text)
        service = modal.Cls.from_name('istok-corrector', 'QwenCorrector', environment_name='main')()
        progress('Подготавливаем Qwen на сервере; первый запуск может занять несколько минут', 0, 1)
        # No retries, including when spawn succeeds but its response is lost.
        call = service.suggest.spawn(text, self.deployment_id)
        store.save('correction-call', {'id': 'correction-call:' + identifier,
                                     'call_id': call.object_id, 'deployment_id': self.deployment_id})
        try:
            envelope = call.get(timeout=600)
        except TimeoutError:
            try: call.cancel(terminate_containers=True)
            except Exception: pass
            raise AccessError('Qwen не ответила вовремя. Повтор автоматически не запускается; исход зарезервирован.',
                              'correction_timeout', 503) from None
        if not isinstance(envelope, dict) or envelope.get('deployment_id') != self.deployment_id or envelope.get('model_digest') != self.model_digest:
            raise ValueError('Версия Qwen не совпала. Предложения не применены.')
        result = envelope.get('result')
        if not isinstance(result, dict) or result.get('text_sha256') != sha256(text.encode()).hexdigest():
            raise ValueError('Ответ Qwen относится к другому тексту.')
        progress('Сохраняем предложения для ручной проверки', 1, 1)
        return result
