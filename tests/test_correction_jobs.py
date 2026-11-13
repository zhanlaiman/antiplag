from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from plagiarism.correction_jobs import CorrectionQuota, internal_id, ModalCorrector
from plagiarism.access import AccessError


class CorrectionQuotaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'quota.sqlite3'
        self.now = datetime(2026, 11, 13, tzinfo=timezone.utc)
        self.quota = CorrectionQuota(self.database, now=lambda: self.now)

    def test_replay_restart_conflict_and_no_quota_reset(self):
        guest = 'a' * 64; identifier = 'b' * 32
        self.assertTrue(self.quota.admit(guest, identifier, 'Текст.'))
        restarted = CorrectionQuota(self.database, now=lambda: self.now)
        self.assertFalse(restarted.admit(guest, identifier, 'Текст.'))
        with self.assertRaises(AccessError): restarted.admit(guest, identifier, 'Другой текст.')
        self.assertEqual(restarted.remaining(guest)['monthly_remaining'], 9)
        self.assertNotEqual(internal_id(guest, identifier), internal_id('c' * 64, identifier))

    def test_concurrent_global_daily_and_personal_limits_are_atomic(self):
        def reserve(index):
            try: return self.quota.admit(f'{index:064x}', f'{index:032x}', 'Текст.')
            except AccessError: return False
        with ThreadPoolExecutor(max_workers=8) as executor: accepted=list(executor.map(reserve, range(8)))
        self.assertEqual(sum(accepted), 3)
        self.assertEqual(self.quota.remaining('a' * 64)['site_daily_remaining'], 0)
        self.now += timedelta(days=1)
        self.quota.admit('a' * 64, 'a' * 32, 'Текст.'); self.quota.admit('a' * 64, 'b' * 32, 'Текст.')
        with self.assertRaisesRegex(AccessError, 'две проверки'): self.quota.admit('a' * 64, 'c' * 32, 'Текст.')

    def test_monthly_limit_survives_days_and_invalid_input_is_free(self):
        for text in ['', '\ud800', 'a' * 3001]:
            with self.assertRaises(ValueError): self.quota.admit('a' * 64, 'a' * 32, text)
        for index in range(10):
            self.now += timedelta(days=1); self.quota.admit('a' * 64, f'{index:032x}', 'Текст.')
        self.now += timedelta(days=1)
        with self.assertRaisesRegex(AccessError, 'месячный'): self.quota.admit('b' * 64, 'f' * 32, 'Текст.')

    def test_invalid_deployment_never_imports_or_calls_modal(self):
        with self.assertRaises(ValueError): ModalCorrector('bad', 'a' * 64)


if __name__ == '__main__': unittest.main()
