from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts import start


class StartTests(unittest.TestCase):
    def test_cloud_configuration_selects_client_environment_without_local_probe(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / '.runtime'
            python = runtime / 'modal-venv/bin/python'
            python.parent.mkdir(parents=True)
            python.touch()
            (runtime / 'cloud-models.json').write_text('{}')
            app = Mock()
            app.wait.return_value = 0
            with patch.object(start, 'ROOT', root), \
                 patch.object(start.shutil, 'which') as which, \
                 patch.object(start, 'ollama_running') as probe, \
                 patch.object(start.subprocess, 'Popen', return_value=app) as popen, \
                 patch.dict(start.os.environ, {}, clear=True), patch('builtins.print'):
                self.assertEqual(start.main(), 0)
            which.assert_not_called()
            probe.assert_not_called()
            self.assertEqual(popen.call_args.args[0][0], str(python))
            self.assertEqual(popen.call_args.kwargs['env']['ISTOK_MODEL_PROVIDER'], 'modal')
            self.assertEqual(popen.call_args.kwargs['env']['MODAL_CONFIG_PATH'], str(runtime / 'modal-config.toml'))

    def test_disabled_marker_starts_only_site_without_probing_ollama(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".runtime"
            runtime.mkdir()
            (runtime / "local-inference-disabled").touch()
            app = Mock()
            app.wait.return_value = 7

            with patch.object(start, "ROOT", root), \
                 patch.object(start.shutil, "which") as which, \
                 patch.object(start, "ollama_running") as probe, \
                 patch.object(start.subprocess, "Popen", return_value=app) as popen, \
                 patch("builtins.print"):
                self.assertEqual(start.main(), 7)

            which.assert_not_called()
            probe.assert_not_called()
            popen.assert_called_once_with(
                [start.sys.executable, str(root / "server.py"), "--port", "8766"],
                cwd=root,
            )
            self.assertFalse((runtime / "ollama.log").exists())
            app.terminate.assert_not_called()

    def test_without_marker_existing_startup_and_cleanup_are_preserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            daemon = Mock()
            daemon.poll.return_value = None
            app = Mock()
            app.wait.return_value = 0

            with patch.object(start, "ROOT", root), \
                 patch.object(start.shutil, "which", return_value="/fake/ollama"), \
                 patch.object(start, "ollama_running", side_effect=[False, True]), \
                 patch.object(start.subprocess, "Popen", side_effect=[daemon, app]) as popen, \
                 patch("builtins.print"):
                self.assertEqual(start.main(), 0)

            self.assertEqual(popen.call_args_list[0].args[0], ["/fake/ollama", "serve"])
            self.assertEqual(
                popen.call_args_list[0].kwargs["env"]["OLLAMA_MODELS"],
                str(root / ".runtime" / "models"),
            )
            self.assertEqual(len(popen.call_args_list), 2)
            daemon.terminate.assert_called_once_with()
            daemon.wait.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
