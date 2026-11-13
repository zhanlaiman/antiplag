"""Durable, bounded admissions for optional GPU corrections; no paid fallback."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import re
import sqlite3

from plagiarism.access import AccessError
from plagiarism.ai_proofreading import request_payload

MONTHLY_LIMIT = 10
SITE_DAILY_LIMIT = 3
GUEST_DAILY_LIMIT = 2


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

    def admit(self, guest, identifier, text):
        internal_id(guest, identifier); request_payload(text)
        fingerprint = sha256(text.encode()).hexdigest()
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
    def __init__(self, deployment_id, model_digest):
        if not re.fullmatch('[a-f0-9]{64}', deployment_id or '') or not re.fullmatch('[a-f0-9]{64}', model_digest or ''):
            raise ValueError('Нужна проверенная версия Qwen.')
        self.deployment_id, self.model_digest = deployment_id, model_digest

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
