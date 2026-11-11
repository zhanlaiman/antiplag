#!/usr/bin/env python3
"""Start the local app, respecting the persistent Ollama autostart opt-out."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pdf_environment(runtime):
    """Use an explicitly configured existing parser; never install anything."""
    try:
        path = runtime / "pdf-extraction.json"
        if path.stat().st_size > 4096:
            return {}
        config = json.loads(path.read_text(encoding="utf-8"))
        python = Path(config["python"])
        if config.get("engine") == "pdfium" and python.is_absolute() and python.is_file():
            return {"ISTOK_PDF_ALTERNATIVE_ENGINE": "pdfium", "ISTOK_PDFIUM_PYTHON": str(python)}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {}


def ollama_running():
    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
            return isinstance(json.load(response), dict)
    except (URLError, OSError, ValueError):
        return False


def main():
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    child = None
    log = None
    cloud_configured = (runtime / "cloud-models.json").is_file()
    provider = os.environ.get("ISTOK_MODEL_PROVIDER", "modal" if cloud_configured else "local")
    local_inference_disabled = (runtime / "local-inference-disabled").exists() or provider == "modal"
    if local_inference_disabled:
        print("Автозапуск локальной Ollama отключён. Запускается только сайт.", flush=True)
    executable = None if local_inference_disabled else shutil.which("ollama")
    if executable and not ollama_running():
        env = {**os.environ, "OLLAMA_MODELS": str(runtime / "models"),
               "OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1",
               "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0"}
        log = (runtime / "ollama.log").open("a", encoding="utf-8")
        child = subprocess.Popen([executable, "serve"], env=env, stdout=log, stderr=log)
        for _ in range(20):
            if ollama_running() or child.poll() is not None:
                break
            time.sleep(0.5)
    print("Откройте http://127.0.0.1:8766", flush=True)
    python = sys.executable
    app_options = {"cwd": ROOT}
    pdf_options = pdf_environment(runtime)
    if pdf_options:
        environment = dict(os.environ)
        for key, value in pdf_options.items():
            environment.setdefault(key, value)
        app_options["env"] = environment
    if provider == "modal":
        environment = {**app_options.get("env", os.environ), "ISTOK_MODEL_PROVIDER": "modal"}
        environment.setdefault("MODAL_CONFIG_PATH", str(runtime / "modal-config.toml"))
        cloud_python = runtime / "modal-venv" / "bin" / "python"
        if cloud_python.is_file():
            python = str(cloud_python)
        app_options["env"] = environment
    app = subprocess.Popen([python, str(ROOT / "server.py"), "--port", "8766"], **app_options)
    try:
        return app.wait()
    except KeyboardInterrupt:
        app.terminate()
        app.wait()
        return 0
    finally:
        if child and child.poll() is None:
            child.terminate()
            child.wait()
        if log:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
