#!/usr/bin/env python3
"""Start the local app and reuse an existing Ollama server when available."""

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
    executable = shutil.which("ollama")
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
    app = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", "8766"], cwd=ROOT)
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
