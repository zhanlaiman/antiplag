import ast
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from cloud import modal_evaluation as runner


class ModalPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in runner.UPLOAD_FILES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic code\n", encoding="utf-8")
        (self.root / "data/examples.json").write_text(json.dumps([{"synthetic": True}]), encoding="utf-8")
        (self.root / "data/retrieval_examples.json").write_text(json.dumps({"synthetic": True}), encoding="utf-8")

    def test_manifest_includes_only_explicit_files(self):
        private = self.root / ".runtime/private.sqlite3"
        private.parent.mkdir()
        private.write_text("private")
        (self.root / "plagiarism/notes.txt").write_text("private")
        manifest = runner.upload_manifest(self.root)
        self.assertEqual({item["path"] for item in manifest}, set(runner.UPLOAD_FILES))
        self.assertNotIn("private", json.dumps(manifest))
        self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest))

    def test_upload_covers_direct_project_import_dependencies(self):
        allowed = set(runner.UPLOAD_FILES)
        self.assertEqual(len(allowed), 18)
        self.assertTrue({"plagiarism/llm.py", "plagiarism/cloud_transport.py",
                         "plagiarism/cloud_contract.py"}.issubset(allowed))
        for relative in allowed:
            if not relative.endswith(".py"):
                continue
            source = (runner.PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                modules = ([node.module + "." + alias.name for alias in node.names]
                           if node.module in {"plagiarism", "scripts", "cloud"} else [node.module])
                for module in modules:
                    if module.split(".")[0] in {"plagiarism", "scripts", "cloud"}:
                        dependency = module.replace(".", "/") + ".py"
                        self.assertIn(dependency, allowed, f"{relative} imports {module}")

    def test_benchmark_container_forces_local_ollama_before_starting_processes(self):
        manifest = runner.upload_manifest(self.root)
        modules = {"plagiarism": Mock(llm=Mock()),
                   "scripts.benchmark_model": Mock(memory_snapshot=Mock(return_value={}))}
        with patch.dict(os.environ, {"ISTOK_MODEL_PROVIDER": "modal"}), \
             patch.dict(runner.sys.modules, modules), \
             patch.object(runner, "REMOTE_ROOT", str(self.root)), \
             patch.object(runner.sys, "path", list(runner.sys.path)), \
             patch.object(runner.subprocess, "Popen", side_effect=RuntimeError("stop before process")) as process:
            with self.assertRaisesRegex(RuntimeError, "stop before process"):
                runner.run_remote("offline-test", manifest)
            self.assertEqual(os.environ["ISTOK_MODEL_PROVIDER"], "local")
        self.assertEqual(process.call_args.kwargs["env"]["ISTOK_MODEL_PROVIDER"], "local")

    def test_symlink_in_allowlist_is_rejected(self):
        path = self.root / "plagiarism/llm.py"
        path.unlink()
        path.symlink_to(self.root / "data/examples.json")
        with self.assertRaisesRegex(ValueError, "Символическая ссылка"):
            runner.upload_manifest(self.root)

    def test_non_synthetic_dataset_is_rejected(self):
        (self.root / "data/examples.json").write_text(json.dumps([{"synthetic": False}]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "синтетические"):
            runner.upload_manifest(self.root)

    def test_run_identifiers_cannot_escape_cache_directory(self):
        for value in ("../other", "/absolute", "a/b", "", "$(anything)"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runner.validate_run_id(value)
        self.assertEqual(runner.validate_run_id("20261111-test_1"), "20261111-test_1")

    def test_diagnostic_exit_one_preserves_complete_pipeline_json(self):
        output = self.root / "result.json"

        def evaluate(path, run_name):
            self.assertEqual(runner.sys.argv[runner.sys.argv.index("--model") + 1], runner.MODEL)
            output.write_text(json.dumps({"metrics": {"hybrid_qwen": {"errors": 1}}}), encoding="utf-8")
            raise SystemExit(1)

        with patch.object(runner.runpy, "run_path", side_effect=evaluate):
            code, result = runner.invoke_pipeline(self.root, self.root / "cache.json", output)
        self.assertEqual(code, 1)
        self.assertEqual(result["metrics"]["hybrid_qwen"]["errors"], 1)

    def test_selected_model_is_passed_to_existing_pipeline_cli(self):
        output = self.root / "result.json"

        def evaluate(path, run_name):
            self.assertEqual(runner.sys.argv[runner.sys.argv.index("--model") + 1], "qwen3.5:9b")
            output.write_text(json.dumps({"model": "qwen3.5:9b"}), encoding="utf-8")

        with patch.object(runner.runpy, "run_path", side_effect=evaluate):
            code, result = runner.invoke_pipeline(self.root, self.root / "cache.json", output, "qwen3.5:9b")
        self.assertEqual(code, 0)
        self.assertEqual(result["model"], "qwen3.5:9b")

    def test_invalid_model_is_rejected_before_local_or_remote_work(self):
        with patch.object(runner, "upload_manifest") as manifest, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                runner.main(["--model", "unapproved:model"])
        manifest.assert_not_called()
        with patch.object(runner.subprocess, "Popen") as process:
            with self.assertRaises(ValueError):
                runner.run_remote("valid-id", [], "unapproved:model")
        process.assert_not_called()

    def test_default_command_is_a_local_plan_without_sdk_or_remote_execution(self):
        with patch.object(runner, "upload_manifest", return_value=[]), \
             patch.object(runner, "build_app") as build, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "local_plan")
        self.assertEqual(json.loads(output.getvalue())["model"], "qwen3:32b-q4_K_M")
        build.assert_not_called()

    def test_local_plan_reports_selected_model_without_cloud_work(self):
        with patch.object(runner, "upload_manifest", return_value=[]), \
             patch.object(runner, "build_app") as build, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main(["--model", "qwen3.5:9b"]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["model"], "qwen3.5:9b")
        self.assertFalse(plan["persistent_volumes"])
        build.assert_not_called()

    def test_private_function_has_one_gpu_container_and_no_persistent_volume(self):
        sdk = Mock()
        image = sdk.Image.from_registry.return_value.entrypoint.return_value.env.return_value
        image.add_local_file.return_value = image
        sdk.App.return_value.function.return_value = lambda function: function
        with patch.dict("sys.modules", {"modal": sdk}):
            runner.build_app([{"path": "data/examples.json"}])
        settings = sdk.App.return_value.function.call_args.kwargs
        self.assertEqual(settings["gpu"], "L40S")
        self.assertEqual(settings["max_containers"], 1)
        self.assertEqual(settings["min_containers"], 0)
        self.assertTrue(settings["single_use_containers"])
        self.assertFalse(settings["include_source"])
        self.assertNotIn("volumes", settings)
        sdk.Volume.from_name.assert_not_called()


if __name__ == "__main__":
    unittest.main()
