import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from cloud import modal_corrector as service


class ModalCorrectorTests(unittest.TestCase):
    def test_manifest_is_source_only_and_changes_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in service.FILES:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('# source')
            (root / 'private.txt').write_text('private')
            first = service.source_manifest(root)
            self.assertEqual(len(first), 2)
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


if __name__ == '__main__': unittest.main()
