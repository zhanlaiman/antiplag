import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cloud import modal_inference as gateway
from plagiarism import cloud_contract


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in gateway.FILES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# service fixture\n', encoding='utf-8')
        self.benchmark = self.root / 'resources.json'
        self.benchmark.write_text(json.dumps({'runtime': {'installed_models': {'models': [
            {'name': 'qwen3.5:9b', 'digest': 'a' * 64},
            {'name': 'bge-m3:latest', 'digest': 'b' * 64},
            {'name': 'unselected:70b', 'digest': 'c' * 64},
        ]}}}), encoding='utf-8')
        self.uploads = gateway.source_manifest(self.root)
        self.config = gateway.config_from_benchmark(self.benchmark, 'qwen3.5:9b', self.uploads)

    def test_upload_list_excludes_private_files_and_symlinks(self):
        (self.root / 'student.pdf').write_text('private')
        self.assertEqual({x['path'] for x in self.uploads}, set(gateway.FILES))
        target = self.root / gateway.FILES[0]
        target.unlink()
        target.symlink_to(self.root / 'student.pdf')
        with self.assertRaises(ValueError):
            gateway.source_manifest(self.root)

    def test_config_contains_only_selected_verified_models(self):
        self.assertEqual(set(cloud_contract.model_digests(self.config)), {'qwen3.5:9b', 'bge-m3:latest'})
        self.assertEqual(self.config['provider'], 'modal')
        with self.assertRaises(ValueError):
            gateway.config_from_benchmark(self.benchmark, 'missing:9b', self.uploads)

    def test_changed_source_changes_deployment_identity(self):
        (self.root / gateway.FILES[0]).write_text('# changed service\n')
        changed = gateway.config_from_benchmark(self.benchmark, 'qwen3.5:9b', gateway.source_manifest(self.root))
        self.assertNotEqual(changed['deployment_id'], self.config['deployment_id'])

    def test_identity_or_forbidden_operation_never_reaches_ollama(self):
        service = gateway._ServiceCore()
        service._config = self.config
        service._digests = cloud_contract.model_digests(self.config)
        with patch.object(gateway, 'loopback_request') as request:
            with self.assertRaises(RuntimeError):
                service.request('/api/embed', {}, 'd' * 64)
            with self.assertRaises(cloud_contract.CloudContractError):
                service.request('/api/pull', {}, self.config['deployment_id'])
            request.assert_not_called()

    def test_embed_response_contains_verified_runtime_identity(self):
        service = gateway._ServiceCore()
        service._config = self.config
        service._digests = cloud_contract.model_digests(self.config)
        payload = {'model': 'bge-m3', 'input': ['Тестовый фрагмент'], 'truncate': False, 'keep_alive': '2m'}
        response = {'embeddings': [[0.5, 0.5]]}
        with patch.object(gateway, 'loopback_request', return_value=response) as request:
            envelope = service.request('/api/embed', payload, self.config['deployment_id'])
        self.assertEqual(cloud_contract.validate_envelope(envelope, self.config), response)
        self.assertEqual(request.call_args.args[1]['model'], 'bge-m3:latest')


if __name__ == '__main__':
    unittest.main()
