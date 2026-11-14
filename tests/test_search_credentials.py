import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import server
from plagiarism import access, search_credentials


class SearchCredentialsTests(unittest.TestCase):
    def test_local_setup_stores_private_file_and_never_returns_the_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ISTOK_RUNTIME_DIR': directory}, clear=True):
            key = 'tvly-not-a-real-key-for-offline-tests'
            handler = server.Handler.__new__(server.Handler)
            handler.server = SimpleNamespace(saas=None)
            result = handler.route('POST', '/api/local-search-config', {'provider': 'tavily', 'api_key': key})
            self.assertEqual(result, {'saved': True, 'provider': 'tavily'})
            self.assertNotIn(key, json.dumps(result))
            file = Path(directory) / 'search-api.json'
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(search_credentials.key_for('tavily'), key)
            self.assertEqual(search_credentials.selected_provider(), 'tavily')

    def test_public_mode_cannot_change_or_serve_operator_credentials(self):
        handler = server.Handler.__new__(server.Handler)
        handler.server = SimpleNamespace(saas=True)
        with patch.object(search_credentials, 'save_local') as save:
            for method in ('GET', 'POST'):
                with self.assertRaises(access.AccessError):
                    handler.route(method, '/api/local-search-config', {})
            for path in ('/setup-search.html', '/setup-search.js'):
                with self.assertRaises(access.AccessError):
                    handler.static(path)
            save.assert_not_called()

    def test_insecure_or_symlinked_secret_files_are_not_used(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ISTOK_RUNTIME_DIR': directory}, clear=True):
            file = Path(directory) / 'search-api.json'
            file.write_text(json.dumps({'provider': 'tavily', 'api_key': 'test-private-key'}))
            file.chmod(0o644)
            self.assertEqual(search_credentials.key_for('tavily'), '')
            file.unlink()
            other = Path(directory) / 'other.json'
            other.write_text(json.dumps({'provider': 'tavily', 'api_key': 'test-private-key'}))
            other.chmod(0o600)
            file.symlink_to(other)
            self.assertEqual(search_credentials.key_for('tavily'), '')

    def test_environment_overrides_file_and_unknown_provider_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ISTOK_RUNTIME_DIR': directory}, clear=True):
            search_credentials.save_local('tavily', 'tvly-not-a-real-key-for-offline-tests')
            with patch.dict(os.environ, {'ISTOK_TAVILY_API_KEY': 'environment-key', 'ISTOK_SEARCH_PROVIDER': 'untrusted'}):
                self.assertEqual(search_credentials.key_for('tavily'), 'environment-key')
                self.assertEqual(search_credentials.selected_provider(), 'unavailable')

    def test_malformed_key_is_rejected_without_creating_a_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ISTOK_RUNTIME_DIR': directory}, clear=True):
            for key in ('', 'wrong-provider-key', 'tvly-good-looking\nAuthorization: injected'):
                with self.assertRaises(ValueError):
                    search_credentials.save_local('tavily', key)
            self.assertFalse((Path(directory) / 'search-api.json').exists())

    def test_google_credential_is_separate_private_and_requires_explicit_provider_switch(self):
        with tempfile.TemporaryDirectory()as directory,patch.dict(os.environ,{'ISTOK_RUNTIME_DIR':directory},clear=True):
            search_credentials.save_local('tavily','tvly-not-a-real-key-for-offline-tests')
            path=Path(directory)/'serpapi-api.json';path.write_text(json.dumps({'provider':'serpapi','api_key':'a'*64}));path.chmod(0o600)
            self.assertEqual(search_credentials.key_for('serpapi'),'a'*64)
            self.assertEqual(search_credentials.selected_provider(),'tavily')
            with patch.dict(os.environ,{'ISTOK_SEARCH_PROVIDER':'serpapi'}):self.assertEqual(search_credentials.selected_provider(),'serpapi')
            path.chmod(0o644);self.assertEqual(search_credentials.key_for('serpapi'),'')

    def test_yandex_uses_the_existing_serpapi_credential_only_when_selected(self):
        with patch.dict(os.environ,{'ISTOK_SERPAPI_API_KEY':'offline-key','ISTOK_SEARCH_PROVIDER':'serpapi-yandex'}):
            self.assertEqual(search_credentials.key_for('serpapi-yandex'),'offline-key')
            self.assertEqual(search_credentials.selected_provider(),'serpapi-yandex')


if __name__ == '__main__':
    unittest.main()
