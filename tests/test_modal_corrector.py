import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import json
from contextlib import nullcontext, redirect_stdout
import io
import sys
from types import SimpleNamespace
from cloud import modal_corrector as service


class ModalCorrectorTests(unittest.TestCase):
    def test_manifest_is_source_only_and_changes_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in service.FILES:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('# source')
            (root / 'private.txt').write_text('private')
            first = service.source_manifest(root)
            self.assertEqual(len(first), 3)
            target = root / service.FILES[0]
            target.write_text('# changed')
            self.assertNotEqual(service.deployment_id(first), service.deployment_id(service.source_manifest(root)))
            target.unlink(); target.symlink_to(root / 'private.txt')
            with self.assertRaises(ValueError): service.source_manifest(root)

    def test_version_and_input_are_checked_before_inference(self):
        core = service._Core(); core._identity = 'a' * 64
        with patch.object(service, 'loopback') as model:
            with self.assertRaises(RuntimeError): core.suggest('Текст', 'b' * 64)
            with self.assertRaises(ValueError): core.suggest('x' * 3001, 'a' * 64)
            model.assert_not_called()

    def test_fixed_model_and_parsed_grounded_response(self):
        core = service._Core(); core._identity = 'a' * 64
        response = {'done': True, 'done_reason': 'stop', 'message': {'content': '{"edits":[]}'}}
        with patch.object(service, 'loopback', return_value=response) as model:
            result = core.suggest('Обычный текст.', core._identity)
        self.assertEqual(model.call_args.args[0], '/api/chat')
        self.assertEqual(model.call_args.args[1]['model'], service.MODEL)
        self.assertEqual(result['model_digest'], service.DIGEST)
        self.assertEqual(result['result']['issues'], [])

    def test_rewrite_checks_identity_and_input_before_inference(self):
        core = service._Core(); core._identity = 'a' * 64
        with patch.object(service, 'loopback') as model:
            with self.assertRaises(RuntimeError): core.rewrite_fragment('Текст.', [], 'b' * 64)
            with self.assertRaises(ValueError): core.rewrite_fragment('x' * 1001, [], core._identity)
            model.assert_not_called()

    def test_rewrite_returns_separate_bound_complete_result(self):
        core = service._Core(); core._identity = 'a' * 64
        text = 'Наш опыт завершился в 2026 году.'
        def answer(path, payload, timeout):
            self.assertEqual(path, '/api/chat')
            self.assertEqual(payload['model'], service.MODEL)
            masked = json.loads(payload['messages'][1]['content'])['text']
            return {'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps({
                'rewritten_text': masked.replace('Наш опыт завершился', 'Мы завершили опыт')})}}
        with patch.object(service, 'loopback', side_effect=answer) as model:
            envelope = core.rewrite_fragment(text, [], core._identity)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(envelope['deployment_id'], core._identity)
        self.assertEqual(envelope['model_digest'], service.DIGEST)
        self.assertEqual(envelope['result']['rewritten_text'], 'Мы завершили опыт в 2026 году.')
        self.assertEqual(envelope['result']['mode'], 'rewrite_own')
        self.assertEqual(envelope['result']['version'], 'qwen-rewrite-own-v1')
        self.assertEqual(envelope['result']['offset_encoding'], 'utf-16')
        self.assertIs(envelope['result']['requires_review'], True)

    def test_rewrite_rejects_truncation_or_damaged_literals_without_retry(self):
        core = service._Core(); core._identity = 'a' * 64
        for response in (
                {'done': True, 'done_reason': 'length', 'message': {'content': '{"rewritten_text":"Текст."}'}},
                {'done': True, 'done_reason': 'stop', 'message': {'content': '{"rewritten_text":"Опыт в 2027 году."}'}}):
            with self.subTest(response=response), patch.object(service, 'loopback', return_value=response) as model:
                with self.assertRaises(ValueError): core.rewrite_fragment('Опыт в 2026 году.', [], core._identity)
                self.assertEqual(model.call_count, 1)

    def test_versioned_private_build_preserves_resource_bounds_and_registers_both_methods(self):
        captures = {}
        class Image:
            def entrypoint(self, value): return self
            def env(self, value): captures['env'] = value; return self
            def add_local_file(self, *args, **kwargs): return self
        class App:
            def __init__(self, name, **kwargs): captures['app'] = name
            def cls(self, **kwargs):
                captures['resources'] = kwargs
                return lambda implementation: implementation
        fake = SimpleNamespace(Image=SimpleNamespace(from_registry=lambda *a, **k: Image()), App=App,
                               enter=lambda: lambda f: f, method=lambda: lambda f: f, exit=lambda: lambda f: f)
        with patch.dict(sys.modules, {'modal': fake}):
            for app_name in (service.APP, service.REWRITE_APP):
                application, implementation = service.build_service(service.source_manifest(), app_name)
                self.assertEqual(captures['app'], app_name)
                self.assertEqual(captures['env']['ISTOK_CORRECTOR_APP'], app_name)
                self.assertIs(implementation.suggest, service._Core.suggest)
                self.assertIs(implementation.rewrite_fragment, service._Core.rewrite_fragment)
                self.assertEqual(captures['resources']['gpu'], 'L40S')
                self.assertEqual(captures['resources']['min_containers'], 0)
                self.assertEqual(captures['resources']['max_containers'], 1)
                self.assertEqual(captures['resources']['retries'], 0)
                self.assertEqual(captures['resources']['timeout'], 180)
        with self.assertRaises(ValueError): service.build_service([], 'unknown-app')

    def test_versioned_cli_metadata_never_overwrites_legacy_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / '.runtime').mkdir()
            old = root / '.runtime/corrector-deployment.json'
            old.write_text('legacy metadata')
            fake = SimpleNamespace(enable_output=nullcontext)
            application = SimpleNamespace(deploy=lambda **kwargs: None)
            implementation = SimpleNamespace(object_id='synthetic-class-id')
            manifest = service.source_manifest()
            with patch.dict(sys.modules, {'modal': fake}), patch.object(service, 'ROOT', root), \
                    patch.object(service, 'source_manifest', return_value=manifest), \
                    patch.object(service, 'build_service', return_value=(application, implementation)) as build, \
                    patch.object(sys, 'argv', ['corrector', '--app', service.REWRITE_APP, '--deploy']), \
                    redirect_stdout(io.StringIO()):
                service.main()
            self.assertEqual(old.read_text(), 'legacy metadata')
            new = root / '.runtime/corrector-rewrite-deployment.json'
            self.assertEqual(json.loads(new.read_text())['app'], service.REWRITE_APP)
            self.assertEqual(new.stat().st_mode & 0o777, 0o600)
            build.assert_called_once_with(manifest, service.REWRITE_APP)


if __name__ == '__main__': unittest.main()
