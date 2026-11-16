from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from cloud import modal_web
from plagiarism import ai_rewriting
from plagiarism.access import AccessError
from plagiarism.correction_jobs import (CorrectionQuota, ModalCorrector, correction_request,
    internal_id, qualified_rewrite_capability, validate_rewrite_capability, rewrite_capability_json)
from plagiarism.storage import Store
from tests import test_integrated_beta as integrated_fixture

MODEL = 'a' * 64
LEGACY = 'b' * 64
DEPLOYMENT = 'c' * 64
TEXT = 'Иванов изучил 17 примеров.'
LITERALS = ['Иванов']


def fields():
    return {'schema_version': 1, 'app': 'istok-corrector-rewrite-v1', 'class_name': 'QwenCorrector',
            'environment': 'main', 'deployment_id': DEPLOYMENT, 'model_digest': MODEL,
            'service_manifest_sha256': 'd' * 64, 'mode': ai_rewriting.MODE, 'version': ai_rewriting.VERSION,
            'max_characters': 1000, 'max_output_characters': 1500}


def capability():
    return {**fields(), 'qualification_sha256': 'e' * 64}


def native_result(text=TEXT, literals=LITERALS):
    payload = ai_rewriting.request_payload(text, literals)
    masked = json.loads(payload['messages'][1]['content'])['text']
    return ai_rewriting.parse_response(text, {'done': True, 'done_reason': 'stop',
        'message': {'content': json.dumps({'rewritten_text': masked}, ensure_ascii=False)}}, literals)


def sdk():
    call = Mock(object_id='synthetic-call')
    call.get.return_value = {'deployment_id': DEPLOYMENT, 'model_digest': MODEL, 'result': native_result()}
    service = SimpleNamespace(rewrite_fragment=SimpleNamespace(spawn=Mock(return_value=call)),
                              suggest=SimpleNamespace(spawn=Mock(return_value=call)))
    module = SimpleNamespace(Cls=SimpleNamespace(from_name=Mock(return_value=lambda: service)))
    return module, service, call


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))
    path.chmod(0o600)


def saved_descriptor(root):
    proof = {'kind': 'qwen-rewrite-qualification-v1', 'status': 'PASS', 'ready_for_public': True,
             'capability': fields(), 'model_smoke': {'complete': True, **{k: fields()[k] for k in
                ('deployment_id', 'model_digest', 'mode', 'version')}}, 'independent_review_sha256': 'f' * 64}
    proof_path = root / '.runtime/synthetic-qualification.json'
    write_json(proof_path, proof)
    descriptor = {**fields(), 'qualification_ref': {'path': str(proof_path),
                   'sha256': sha256(proof_path.read_bytes()).hexdigest()}}
    write_json(root / '.runtime/corrector-rewrite-deployment.json', descriptor)
    return descriptor, proof, proof_path


class RewriteCapabilityTests(unittest.TestCase):
    def test_runtime_json_ambiguity_nonfinite_and_size_disable_cpu_only(self):
        raw = json.dumps(capability())
        self.assertEqual(rewrite_capability_json(raw, MODEL), capability())
        for value in (None, '', '[]', ' ' * 65537, raw[:-1], raw[:-1] + ',"schema_version":1}',
                      raw.replace('"schema_version": 1', '"schema_version": NaN')):
            with self.subTest(value=type(value).__name__):
                self.assertIsNone(rewrite_capability_json(value, MODEL))

    def test_two_argument_constructor_remains_grammar_only_and_metadata_is_copied(self):
        self.assertIsNone(ModalCorrector(LEGACY, MODEL).rewrite_capability)
        value = capability()
        client = ModalCorrector(LEGACY, MODEL, value)
        self.assertEqual(client.deployment_id, LEGACY)
        self.assertEqual(client.rewrite_capability['deployment_id'], DEPLOYMENT)
        value['deployment_id'] = '0' * 64
        self.assertEqual(client.rewrite_capability['deployment_id'], DEPLOYMENT)

    def test_all_cpu_binding_mismatches_disable_without_import_or_probe(self):
        mutations = {'schema_version': True, 'app': 'istok-corrector', 'class_name': 'Wrong',
                     'environment': 'other', 'deployment_id': 'bad', 'model_digest': '0' * 64,
                     'service_manifest_sha256': None, 'mode': 'grammar', 'version': 'old',
                     'max_characters': True, 'max_output_characters': 1501, 'qualification_sha256': ''}
        for key, value in mutations.items():
            with self.subTest(key=key):
                client = ModalCorrector(LEGACY, MODEL, {**capability(), key: value})
                self.assertIsNone(client.rewrite_capability)
                with self.assertRaises(AccessError):
                    client.rewrite_fragment(TEXT, LITERALS, None, '1' * 32, lambda *a: None)
        for value in (None, {}, [], {**capability(), 'unexpected': True}):
            self.assertIsNone(validate_rewrite_capability(value, MODEL))

    def test_actual_private_qualification_bytes_bind_all_fields_and_strip_path(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            descriptor, proof, path = saved_descriptor(root)
            result = modal_web.load_rewrite_capability(root, MODEL)
            self.assertEqual(result, {**fields(), 'qualification_sha256': descriptor['qualification_ref']['sha256']})
            self.assertNotIn(str(root), json.dumps(result))
            self.assertIsNone(modal_web.load_rewrite_capability(root, '0' * 64))
            for key, value in [('kind', 'other'), ('status', 'FAIL'), ('ready_for_public', 1),
                               ('independent_review_sha256', 'bad')]:
                bad = {**proof, key: value}
                self.assertIsNone(qualified_rewrite_capability(descriptor, bad, MODEL))
            for key in ('deployment_id', 'model_digest', 'mode', 'version', 'complete'):
                bad = copy.deepcopy(proof); bad['model_smoke'][key] = 'wrong'
                self.assertIsNone(qualified_rewrite_capability(descriptor, bad, MODEL))
            for key, value in fields().items():
                bad = copy.deepcopy(proof); bad['capability'][key] = not value if type(value) is int else 'wrong'
                self.assertIsNone(qualified_rewrite_capability(descriptor, bad, MODEL), key)
            path.write_text('{}')
            self.assertIsNone(modal_web.load_rewrite_capability(root, MODEL))

    def test_missing_oversize_public_symlink_duplicate_or_malformed_metadata_disables(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.assertIsNone(modal_web.load_rewrite_capability(root, MODEL))
            descriptor, _, proof = saved_descriptor(root)
            config = root / '.runtime/corrector-rewrite-deployment.json'
            for raw in ('{', '[]', '{"x":1,"x":2}', '{"x":NaN}', ' ' * 65537):
                config.write_text(raw)
                self.assertIsNone(modal_web.load_rewrite_capability(root, MODEL))
            write_json(config, descriptor); proof.chmod(0o644)
            self.assertIsNone(modal_web.load_rewrite_capability(root, MODEL))
            proof.chmod(0o600); config.unlink(); config.symlink_to(proof)
            self.assertIsNone(modal_web.load_rewrite_capability(root, MODEL))

    def test_builder_injects_only_verified_sanitized_capability_and_keeps_legacy_ids(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); descriptor, _, proof = saved_descriptor(root)
            write_json(root / '.runtime/corrector-deployment.json', {'ready_for_public': True,
                'deployment_id': LEGACY, 'model_digest': MODEL, 'app': 'istok-corrector',
                'class_name': 'QwenCorrector', 'max_containers': 1, 'persistent_weights': False})
            module = MagicMock()
            with patch.dict('sys.modules', {'modal': module}), patch.object(modal_web, 'ROOT', root):
                modal_web.build_app(root, 'synthetic-tavily-key')
                secret = module.Secret.from_dict.call_args.args[0]
                self.assertEqual(secret['ISTOK_CORRECTOR_DEPLOYMENT'], LEGACY)
                self.assertEqual(secret['ISTOK_CORRECTOR_MODEL_DIGEST'], MODEL)
                self.assertEqual(json.loads(secret['ISTOK_CORRECTOR_REWRITE_CAPABILITY'])['deployment_id'], DEPLOYMENT)
                self.assertNotIn(str(root), secret['ISTOK_CORRECTOR_REWRITE_CAPABILITY'])
                proof.write_text('{}')
                modal_web.build_app(root, 'synthetic-tavily-key')
                self.assertNotIn('ISTOK_CORRECTOR_REWRITE_CAPABILITY', module.Secret.from_dict.call_args.args[0])


class RewriteClientTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'store.sqlite3')
        self.client = ModalCorrector(LEGACY, MODEL, capability())
        self.module, self.service, self.call = sdk()

    def run_client(self, identifier='1' * 32):
        with patch.dict('sys.modules', {'modal': self.module}):
            return self.client.rewrite_fragment(TEXT, LITERALS, self.store, identifier, lambda *a: None)

    def test_exact_versioned_three_argument_spawn_and_durable_call(self):
        result = self.run_client()
        self.module.Cls.from_name.assert_called_once_with('istok-corrector-rewrite-v1', 'QwenCorrector', environment_name='main')
        self.service.rewrite_fragment.spawn.assert_called_once_with(TEXT, LITERALS, DEPLOYMENT)
        self.service.suggest.spawn.assert_not_called()
        self.assertEqual(result['mode'], ai_rewriting.MODE)
        self.assertEqual(self.store.get('correction-call', 'correction-call:' + '1' * 32)['deployment_id'], DEPLOYMENT)

    def test_invalid_input_is_refused_before_fake_sdk_is_touched(self):
        with patch.dict('sys.modules', {'modal': self.module}), self.assertRaises(ValueError):
            self.client.rewrite_fragment('x' * 1001, [], self.store, '1' * 32, lambda *a: None)
        self.module.Cls.from_name.assert_not_called()

    def test_every_envelope_result_binding_and_unicode_bounds_are_checked(self):
        original = self.call.get.return_value
        variants = [{**original, 'deployment_id': LEGACY}, {**original, 'model_digest': '0' * 64}]
        for key, value in [('mode', 'grammar'), ('version', 'old'), ('text_sha256', '0' * 64),
                           ('protected_literals_sha256', '0' * 64), ('requires_review', 1),
                           ('offset_encoding', 'utf-8'), ('offset_encoding', None),
                           ('rewritten_text', 'x' * 1501), ('rewritten_text', '\ud800'),
                           ('rewritten_text', ''), ('rewritten_text', '\x00')]:
            variants.append({**original, 'result': {**original['result'], key: value}})
        for index, value in enumerate(variants):
            with self.subTest(value=list(value)):
                self.call.get.return_value = value
                with self.assertRaises(ValueError): self.run_client(f'{index + 100:032x}')

    def test_timeout_and_unknown_spawn_never_retry_or_fall_back(self):
        self.call.get.side_effect = TimeoutError()
        with self.assertRaises(AccessError) as error: self.run_client()
        self.assertEqual(error.exception.code, 'correction_timeout')
        self.assertEqual(self.service.rewrite_fragment.spawn.call_count, 1)
        self.call.cancel.assert_called_once_with(terminate_containers=True)
        self.service.suggest.spawn.assert_not_called()
        self.service.rewrite_fragment.spawn.reset_mock()
        self.service.rewrite_fragment.spawn.side_effect = RuntimeError('synthetic unknown spawn outcome')
        with self.assertRaises(RuntimeError): self.run_client()
        self.assertEqual(self.service.rewrite_fragment.spawn.call_count, 1)

    def test_legacy_client_keeps_old_app_two_argument_suggest(self):
        self.call.get.return_value = {'deployment_id': LEGACY, 'model_digest': MODEL,
            'result': {'text_sha256': sha256(TEXT.encode()).hexdigest(), 'issues': []}}
        with patch.dict('sys.modules', {'modal': self.module}):
            self.client(TEXT, self.store, '2' * 32, lambda *a: None)
        self.module.Cls.from_name.assert_called_with('istok-corrector', 'QwenCorrector', environment_name='main')
        self.service.suggest.spawn.assert_called_once_with(TEXT, LEGACY)


class RewriteQuotaTests(unittest.TestCase):
    def test_mixed_modes_share_atomic_site_personal_and_monthly_counts(self):
        with TemporaryDirectory() as directory:
            today = [datetime(2026, 11, 16, tzinfo=timezone.utc)]
            q = CorrectionQuota(Path(directory) / 'beta.sqlite3', now=lambda: today[0])
            def admit(i):
                try:
                    q.admit(f'{i:064x}', f'{i:032x}', TEXT, **({'mode': ai_rewriting.MODE,
                        'version': ai_rewriting.VERSION, 'protected_literals': LITERALS} if i % 2 else {}))
                    return True
                except AccessError: return False
            with ThreadPoolExecutor(max_workers=8) as pool: self.assertEqual(sum(pool.map(admit, range(8))), 3)
            today[0] += timedelta(days=1)
            self.assertTrue(q.admit('f' * 64, 'a' * 32, TEXT))
            self.assertTrue(q.admit('f' * 64, 'b' * 32, TEXT, mode=ai_rewriting.MODE,
                                   version=ai_rewriting.VERSION, protected_literals=LITERALS))
            with self.assertRaises(AccessError): q.admit('f' * 64, 'c' * 32, TEXT)
            self.assertFalse(q.admit('f' * 64, 'b' * 32, TEXT, mode=ai_rewriting.MODE,
                                    version=ai_rewriting.VERSION, protected_literals=LITERALS))
            with self.assertRaises(AccessError) as error: q.admit('f' * 64, 'b' * 32, TEXT)
            self.assertEqual(error.exception.status, 409)
            for i in range(5):
                today[0] += timedelta(days=1)
                self.assertTrue(q.admit('f' * 64, f'{i + 100:032x}', TEXT))
            today[0] += timedelta(days=1)
            with self.assertRaises(AccessError) as error: q.admit('a' * 64, 'd' * 32, TEXT)
            self.assertEqual(error.exception.code, 'correction_monthly_limit')
            self.assertEqual(q.remaining('f' * 64)['monthly_remaining'], 0)

    def test_legacy_hash_3000_and_invalid_rewrite_never_add_rows(self):
        with TemporaryDirectory() as directory:
            q = CorrectionQuota(Path(directory) / 'beta.sqlite3')
            q.admit('a' * 64, '1' * 32, 'я' * 3000)
            with closing(q.connect()) as db:
                self.assertEqual(db.execute('SELECT fingerprint FROM ai_correction_checks').fetchone()[0], sha256(('я' * 3000).encode()).hexdigest())
            for text, names in [('x' * 1001, []), ('[[ISTOK_RW_bad]]', []), ('aaa', ['aa'])]:
                with self.assertRaises(ValueError): q.admit('b' * 64, '2' * 32, text,
                    mode=ai_rewriting.MODE, version=ai_rewriting.VERSION, protected_literals=names)
            self.assertEqual(q.remaining('b' * 64)['monthly_remaining'], 9)
            self.assertEqual(correction_request(TEXT)[0], {'text': TEXT})


class RewriteHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        integrated_fixture.IntegratedBetaTests.setUpClass()

    def setUp(self):
        self.fx = integrated_fixture.IntegratedBetaTests()
        self.fx.setUp()
        def cleanup():
            try: self.fx.tearDown()
            finally: self.fx.doCleanups()
        self.addCleanup(cleanup)
        self.browser = self.fx.browser()
        self.user = self.fx.register(self.browser)
        self.client = ModalCorrector(LEGACY, MODEL, capability())
        self.fx.service.beta.corrector = self.client
        self.module, self.service, self.call = sdk()
        mock = patch.dict('sys.modules', {'modal': self.module}); mock.start(); self.addCleanup(mock.stop)

    def body(self, identifier='1' * 32, **extra):
        return {'request_id': identifier, 'mode': ai_rewriting.MODE, 'version': ai_rewriting.VERSION,
                'text': TEXT, 'protected_literals': LITERALS, 'ai_consent': True, **extra}

    def wait_job(self, identifier='1' * 32):
        for _ in range(200):
            status, value, _ = self.browser.request('/api/correction-jobs/' + identifier)
            self.assertEqual(status, 200, value)
            if value['job']['status'] in ('complete', 'failed'): return value['job']
            time.sleep(.01)
        self.fail('Synthetic job did not settle')

    def test_public_contract_is_identical_cpu_only_with_or_without_capability(self):
        for client, enabled in [(self.client, True), (ModalCorrector(LEGACY, MODEL), False), (None, False)]:
            self.fx.service.beta.corrector = client
            session = self.browser.request('/api/session')[1]['beta']['ai_correction']
            quota = self.browser.request('/api/correction-quota')[1]
            self.assertEqual(session, quota)
            self.assertEqual(quota['max_characters'], 3000)
            self.assertEqual(quota['rewrite'], {'enabled': enabled, 'version': ai_rewriting.VERSION,
                'max_characters': 1000, 'max_output_characters': 1500})
        self.module.Cls.from_name.assert_not_called()

    def test_complete_replay_archive_foreign_restart_and_disabled_conflict_never_call_again(self):
        status, value, _ = self.browser.request('/api/correction-jobs', self.body())
        self.assertEqual(status, 200, value)
        job = self.wait_job(); self.assertEqual(job['status'], 'complete', job)
        record = job['result']['correction']; cid = record['id']
        before = self.browser.request('/api/correction-quota')[1]
        self.fx.service.beta.corrector = None
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[1]['job']['result'], job['result'])
        self.assertEqual(self.browser.request('/api/corrections/' + cid)[1]['correction'], record)
        foreign = self.fx.browser(); self.fx.register(foreign, 'other_user')
        self.assertEqual(foreign.request('/api/correction-jobs/' + '1' * 32)[0], 404)
        self.assertEqual(foreign.request('/api/corrections/' + cid)[0], 404)
        for change in ({'mode': 'grammar'}, {'text': TEXT + ' Далее.'}, {'protected_literals': []}):
            self.assertEqual(self.browser.request('/api/correction-jobs', self.body(**change))[0], 409)
        self.assertEqual(self.browser.request('/api/data/correction/' + cid + '/trash', {})[0], 200)
        self.assertTrue(self.browser.request('/api/correction-jobs', self.body())[1]['job']['result']['correction_removed'])
        self.assertEqual(self.browser.request('/api/data/correction/' + cid + '/restore', {})[0], 200)
        self.fx.stop(); self.fx.start()
        self.assertIsNone(self.fx.service.beta.corrector)
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[1]['job']['result']['correction'], record)
        after = self.browser.request('/api/correction-quota')[1]
        self.assertEqual(after['monthly_remaining'], before['monthly_remaining'])
        self.assertEqual(self.service.rewrite_fragment.spawn.call_count, 1)
        self.assertEqual(self.fx.calls, [])

    def test_invalid_consent_trust_input_and_disabled_new_request_reserve_no_ai_quota(self):
        for change in ({'ai_consent': False}, {'text': 'x' * 1001}, {'text': 'aaa', 'protected_literals': ['aa']},
                       {'text': '[[ISTOK_RW_bad]]', 'protected_literals': []}, {'version': 'old'}, {'extra': True}):
            status = self.browser.request('/api/correction-jobs', self.body(**change))[0]
            self.assertEqual(status, 400, change)
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body(), extra={'Origin': 'https://foreign.example'})[0], 403)
        self.fx.service.beta.corrector = ModalCorrector(LEGACY, MODEL)
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[0], 503)
        quota = self.browser.request('/api/correction-quota')[1]
        self.assertEqual((quota['personal_daily_remaining'], quota['site_daily_remaining'], quota['monthly_remaining']), (2, 3, 10))
        self.module.Cls.from_name.assert_not_called()

    def test_unknown_spawn_is_durable_failed_no_retry_after_restart_or_disable(self):
        self.service.rewrite_fragment.spawn.side_effect = RuntimeError('synthetic unknown outcome')
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[0], 200)
        self.assertEqual(self.wait_job()['status'], 'failed')
        self.fx.service.beta.corrector = None
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[1]['job']['status'], 'failed')
        self.fx.stop(); self.fx.start()
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[1]['job']['status'], 'failed')
        self.assertEqual(self.service.rewrite_fragment.spawn.call_count, 1)
        self.assertEqual(self.browser.request('/api/correction-quota')[1]['personal_daily_remaining'], 1)

    def test_wrong_encoding_fails_before_correction_save_and_replay_never_retries(self):
        self.call.get.return_value['result']['offset_encoding'] = 'utf-8'
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[0], 200)
        self.assertEqual(self.wait_job()['status'], 'failed')
        guest = self.fx.guest(self.browser)
        with self.fx.service.beta.tenants.context(guest) as tenant, tenant.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='correction'").fetchone()[0], 0)
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[1]['job']['status'], 'failed')
        self.assertEqual(self.service.rewrite_fragment.spawn.call_count, 1)
        self.service.suggest.spawn.assert_not_called()
        self.assertEqual(self.browser.request('/api/correction-quota')[1]['personal_daily_remaining'], 1)

    def test_failure_saving_result_preserves_reservation_and_never_repeats_model(self):
        guest = self.fx.guest(self.browser)
        beta = self.fx.service.beta
        with beta.tenants.context(guest) as tenant:
            original = tenant.store.save
            def save(kind, value):
                if kind == 'correction': raise OSError('Synthetic result persistence failure')
                return original(kind, value)
            with patch.object(tenant.store, 'save', side_effect=save):
                self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[0], 200)
                self.assertEqual(self.wait_job()['status'], 'failed')
                beta.corrector = None
                self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[1]['job']['status'], 'failed')
            self.assertIsNotNone(tenant.store.get('model-request', 'model-request:' + internal_id(guest, '1' * 32)))
        self.assertEqual(self.service.rewrite_fragment.spawn.call_count, 1)
        self.assertEqual(self.browser.request('/api/correction-quota')[1]['personal_daily_remaining'], 1)

    def test_reservation_only_recovers_without_capability_reschedule_or_admission(self):
        guest = self.fx.guest(self.browser)
        beta = self.fx.service.beta
        with beta.tenants.context(guest) as tenant:
            fields_, fingerprint = correction_request(TEXT, mode=ai_rewriting.MODE, version=ai_rewriting.VERSION, protected_literals=LITERALS)
            body = {**fields_, 'request_id': '1' * 32, 'ai_consent': True}
            beta.correction_quota.admit(guest, '1' * 32, TEXT, mode=ai_rewriting.MODE, version=ai_rewriting.VERSION, protected_literals=LITERALS)
            remote = internal_id(guest, '1' * 32)
            digest = sha256(json.dumps({'path': '/api/correction', 'body': body}, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            tenant.store.save('model-request', {'id': 'model-request:' + remote, 'path': '/api/correction', 'request_fingerprint': digest})
        beta.corrector = None
        value = self.browser.request('/api/correction-jobs', self.body())
        self.assertEqual(value[0], 200, value)
        self.assertEqual(value[1]['job']['code'], 'job_interrupted')
        self.assertEqual(beta.correction_jobs.coordinator.active, 0)
        self.assertEqual(self.browser.request('/api/correction-quota')[1]['personal_daily_remaining'], 1)
        self.module.Cls.from_name.assert_not_called()

    def test_native_block_while_queued_prevents_rewrite_spawn_without_refund(self):
        entered, release = Event(), Event(); self.addCleanup(release.set)
        grammar_call = Mock(object_id='synthetic-grammar')
        def get(**kwargs):
            entered.set(); self.assertTrue(release.wait(5))
            return {'deployment_id': LEGACY, 'model_digest': MODEL,
                    'result': {'text_sha256': sha256(TEXT.encode()).hexdigest(), 'issues': []}}
        grammar_call.get.side_effect = get
        self.service.suggest.spawn.return_value = grammar_call
        first = {'request_id': '2' * 32, 'text': TEXT, 'ai_consent': True}
        self.assertEqual(self.browser.request('/api/correction-jobs', first)[0], 200)
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[0], 200)
        guest = self.fx.guest(self.browser)
        self.fx.block(self.fx.admin(), self.user['id'])
        self.assertEqual(self.browser.request('/api/correction-jobs', self.body())[0], 403)
        release.set()
        beta = self.fx.service.beta
        with beta.tenants.context(guest) as tenant:
            for _ in range(200):
                job = beta.correction_jobs.get(internal_id(guest, '1' * 32), tenant.store)
                if job['status'] == 'failed': break
                time.sleep(.01)
            self.assertEqual(job['status'], 'failed', job)
        self.service.rewrite_fragment.spawn.assert_not_called()
        self.assertEqual(beta.correction_quota.remaining(guest)['personal_daily_remaining'], 0)


if __name__ == '__main__':
    unittest.main()
