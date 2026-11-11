"""Prepare or explicitly run the synthetic diagnostic on one private Modal GPU."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import time
from threading import Event, Thread
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REMOTE_ROOT = "/opt/istok"
MODEL = "qwen3:32b-q4_K_M"
ALLOWED_MODELS = ("qwen3:4b", "qwen3.5:9b", MODEL)
EMBEDDING_MODEL = "bge-m3"
MODAL_SDK_VERSION = "1.6.3"
OLLAMA_IMAGE = "ollama/ollama:0.34.2@sha256:c715bebf769913db6c82d96f8a8dfee989c4bdfe19fa7d700e3d41ee0ceb5461"
UPLOAD_FILES = (
    "cloud/modal_evaluation.py",
    "plagiarism/__init__.py", "plagiarism/cloud_contract.py", "plagiarism/cloud_transport.py",
    "plagiarism/demo.py", "plagiarism/evaluation.py",
    "plagiarism/jobs.py", "plagiarism/llm.py", "plagiarism/matching.py",
    "plagiarism/pipeline_evaluation.py", "plagiarism/retrieval.py",
    "plagiarism/retrieval_evaluation.py", "plagiarism/source_packs.py", "plagiarism/storage.py",
    "scripts/evaluate_pipeline.py", "scripts/benchmark_model.py",
    "data/examples.json", "data/retrieval_examples.json",
)


def validate_run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Идентификатор прогона должен содержать только буквы, цифры, дефис и подчёркивание.")
    return value


def validate_model(value):
    if value not in ALLOWED_MODELS:
        raise ValueError("Для этого прогона выберите одну из разрешённых моделей Qwen.")
    return value


def upload_manifest(root=PROJECT_ROOT):
    root = Path(root).resolve()
    manifest = []
    for relative in UPLOAD_FILES:
        file = root / relative
        if any(part.is_symlink() for part in (file, *file.parents) if part != root):
            raise ValueError(f"Символическая ссылка запрещена в списке отправки: {relative}")
        if not file.is_file() or not file.resolve().is_relative_to(root):
            raise ValueError(f"Файл списка отправки отсутствует или находится вне проекта: {relative}")
        content = file.read_bytes()
        manifest.append({"path": relative, "sha256": sha256(content).hexdigest(), "bytes": len(content)})
    examples = json.loads((root / "data/examples.json").read_text(encoding="utf-8"))
    retrieval = json.loads((root / "data/retrieval_examples.json").read_text(encoding="utf-8"))
    if (not isinstance(examples, list) or not examples or any(item.get("synthetic") is not True for item in examples)
            or not isinstance(retrieval, dict) or retrieval.get("synthetic") is not True):
        raise ValueError("Облачный прогон принимает только поставляемые синтетические наборы.")
    return manifest


def invoke_pipeline(root, cache, output, model=MODEL):
    """Execute the original CLI and retain complete diagnostic JSON on exit1."""
    model = validate_model(model)
    previous = sys.argv
    sys.argv = [str(root / "scripts/evaluate_pipeline.py"), "--model", model, "--split", "test",
                "--top-k", "5", "--cache", str(cache), "--output", str(output)]
    exit_code = 0
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit as error:
        exit_code = error.code if isinstance(error.code, int) else 1
    finally:
        sys.argv = previous
    if not output.is_file():
        raise RuntimeError(f"Проверка завершилась с кодом {exit_code} и не сохранила отчёт.")
    return exit_code, json.loads(output.read_text(encoding="utf-8"))


def run_remote(run_id, manifest, model=MODEL):
    """Called through the authenticated private Function only after App.run."""
    run_id = validate_run_id(run_id)
    model = validate_model(model)
    root = Path(REMOTE_ROOT)
    if upload_manifest(root) != manifest:
        raise RuntimeError("Контрольные суммы отправленных файлов изменились.")
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    cache_file = runtime / "pipeline-cache.json"
    environment = {**os.environ, "OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_MODELS": str(runtime / "models"),
                   "OLLAMA_NO_CLOUD": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "2",
                   "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
                   "ISTOK_MODEL_PROVIDER": "local", "ISTOK_OLLAMA_URL": "http://127.0.0.1:11434",
                   "ISTOK_EMBED_MODEL": EMBEDDING_MODEL}
    os.environ.update(environment)
    sys.path.insert(0, str(root))
    from plagiarism import llm
    from scripts.benchmark_model import memory_snapshot, record_request, sample_resources

    original_request = llm._request
    result = {"run_id": run_id, "cache_mode": "fresh_ephemeral", "pipeline": None,
              "runtime": {"source_image": OLLAMA_IMAGE, "modal_sdk": MODAL_SDK_VERSION, "requested_model": model,
                          "python": sys.version, "gpu_request": "L40S", "upload_manifest": manifest},
              "resources": {"before": memory_snapshot(), "samples": []}, "requests": []}
    server = subprocess.Popen(["ollama", "serve"], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = Event()
    sampler = None
    started = time.monotonic()
    try:
        for _ in range(60):
            if server.poll() is not None:
                raise RuntimeError("Локальная Ollama в контейнере остановилась при запуске.")
            try:
                original_request("/api/tags", timeout=2)
                break
            except llm.OllamaError:
                time.sleep(1)
        else:
            raise RuntimeError("Локальная Ollama не запустилась за отведённое время.")
        for name in (EMBEDDING_MODEL, model):
            subprocess.run(["ollama", "pull", name], env=environment, check=True, timeout=1800)
        result["runtime"]["ollama_version"] = original_request("/api/version", timeout=3)
        result["runtime"]["installed_models"] = original_request("/api/tags", timeout=3)
        result["runtime"]["model_metadata"] = {
            key: value for key, value in original_request("/api/show", {"model": model}).items()
            if key in {"details", "parameters", "capabilities", "thinking"}
        }
        result["runtime"]["setup_elapsed_seconds"] = round(time.monotonic() - started, 3)
        def recorded(path, payload=None, timeout=180):
            return record_request(original_request, result["requests"], path, payload, timeout)

        llm._request = recorded
        sampler = Thread(target=sample_resources, args=(original_request, result["resources"]["samples"], stop), daemon=True)
        sampler.start()
        result["pipeline_exit_code"], result["pipeline"] = invoke_pipeline(root, cache_file, runtime / "pipeline-result.json", model)
    except Exception as error:
        result["execution_error"] = str(error)
    finally:
        llm._request = original_request
        stop.set()
        if sampler is not None:
            sampler.join(timeout=5)
        result["resources"]["sampling_stopped"] = sampler is None or not sampler.is_alive()
        result["resources"]["samples"] = list(result["resources"]["samples"])
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        result["resources"]["after"] = memory_snapshot()
        result["runtime"]["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def build_app(manifest):
    import modal

    image = modal.Image.from_registry(OLLAMA_IMAGE, add_python="3.11").entrypoint([]).env({"PYTHONPATH": REMOTE_ROOT})
    for entry in manifest:
        image = image.add_local_file(PROJECT_ROOT / entry["path"], f"{REMOTE_ROOT}/{entry['path']}", copy=True)
    app = modal.App("istok-synthetic-evaluation", include_source=False)
    function = app.function(image=image, gpu="L40S", cpu=4, memory=32768,
                            timeout=3600, startup_timeout=600,
                            max_containers=1, min_containers=0, buffer_containers=0,
                            single_use_containers=True, retries=0, include_source=False)(run_remote)
    return modal, app, image, function


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Подготовить или явно запустить синтетическую проверку на Modal")
    parser.add_argument("--run", action="store_true", help="Запустить после проверки аккаунта и лимитов в Modal")
    parser.add_argument("--model", choices=ALLOWED_MODELS, default=MODEL)
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
    args = parser.parse_args(arguments)
    run_id = validate_run_id(args.run_id)
    manifest = upload_manifest()
    if not args.run:
        print(json.dumps({"mode": "local_plan", "model": args.model, "embedding_model": EMBEDDING_MODEL,
                          "image": OLLAMA_IMAGE, "gpu": "L40S", "max_containers": 1,
                          "timeout_seconds": 3600, "cache_mode": "fresh_ephemeral", "persistent_volumes": False,
                          "files": manifest}, ensure_ascii=False, indent=2))
        return 0
    output = PROJECT_ROOT / "data/runs" / f"modal-{run_id}.json"
    instrumentation = output.with_name(output.stem + "-resources.json")
    if output.exists() or instrumentation.exists():
        raise ValueError("Этот идентификатор уже использован. Выберите новый, чтобы сохранить прежний результат.")
    modal, app, image, function = build_app(manifest)
    with modal.enable_output(), app.run(detach=False):
        result = function.remote(run_id, manifest, args.model)
        result["runtime"]["modal_image_id"] = image.object_id
    output.parent.mkdir(parents=True, exist_ok=True)
    if result["pipeline"] is not None:
        output.write_text(json.dumps(result["pipeline"], ensure_ascii=False, indent=2), encoding="utf-8")
    result.pop("pipeline")
    instrumentation.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Измерения сохранены в {instrumentation}")
    if output.is_file():
        print(f"Отчёт проверки сохранён в {output}")
    return int(bool(result.get("execution_error")) or result.get("pipeline_exit_code", 1) != 0)


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT))
    from cloud.modal_evaluation import main as canonical_main

    raise SystemExit(canonical_main())
