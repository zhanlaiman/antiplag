#!/usr/bin/env python3
"""Instrument the existing local eight-case smoke evaluation without changing it."""

import argparse
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plagiarism import evaluation, llm


def record_request(request, records, path, payload=None, timeout=180):
    if path != "/api/chat":
        return request(path, payload, timeout)
    started = time.monotonic()
    entry = {"model": payload.get("model"), "think_requested": payload.get("think"),
             "context_requested": payload.get("options", {}).get("num_ctx")}
    try:
        response = request(path, payload, timeout)
        for key in ("prompt_eval_count", "eval_count", "total_duration", "load_duration",
                    "prompt_eval_duration", "eval_duration", "done_reason"):
            entry[key] = response.get(key)
        thinking = response.get("message", {}).get("thinking", "")
        entry["thinking_characters"] = len(thinking) if isinstance(thinking, str) else None
        entry["status"] = "ok"
        return response
    except Exception as error:
        entry.update(status="error", error=str(error), error_code=getattr(error, "code", "model_error"))
        raise
    finally:
        entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
        records.append(entry)


def sample_resources(request, samples, stop):
    started = time.monotonic()
    while not stop.is_set():
        sample = {"elapsed_seconds": round(time.monotonic() - started, 3)}
        try:
            response = request("/api/ps", timeout=2)
            sample["models"] = [{key: model.get(key) for key in ("name", "size", "size_vram", "context_length")}
                                for model in response.get("models", [])]
        except Exception as error:
            sample["error"] = str(error)
        if sys.platform == "linux":
            sample["host_resources"] = memory_snapshot()
        samples.append(sample)
        stop.wait(1)


def linux_memory_snapshot():
    result = {"source": "/proc/meminfo", "scope": "system"}
    values = {field: None for field in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            field, _, value = line.partition(":")
            parts = value.split()
            if field in values and len(parts) == 2 and parts[0].isdigit():
                values[field] = {"value": int(parts[0]), "unit": parts[1]}
        result.update(status="ok" if all(value is not None for value in values.values()) else "partial", values=values)
    except (OSError, UnicodeError):
        result.update(status="error", error="Не удалось прочитать общесистемную память.")
    return result


def gpu_snapshot():
    result = {"source": "nvidia-smi", "scope": "device"}
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {**result, "status": "not_installed", "devices": []}
    command = [executable, "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"]
    try:
        output = subprocess.run(command, check=True, capture_output=True, text=True, timeout=2).stdout
        devices = []
        for row in csv.reader(io.StringIO(output), skipinitialspace=True):
            if not row:
                continue
            if len(row) != 4:
                raise ValueError("Unexpected GPU response format")
            name, total, used, driver = (value.strip() for value in row)
            devices.append({"name": name, "driver_version": driver,
                            "memory_total": {"value": int(total) if total.isdigit() else None, "unit": "MiB"},
                            "memory_used": {"value": int(used) if used.isdigit() else None, "unit": "MiB"}})
        result.update(status="ok", devices=devices)
    except subprocess.CalledProcessError as error:
        result.update(status="error", error="nvidia-smi завершилась с ошибкой.", returncode=error.returncode)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, csv.Error):
        result.update(status="error", error="Не удалось получить показатели GPU.")
    return result


def memory_snapshot():
    result = {"platform": sys.platform}
    if sys.platform == "darwin":
        for field in ("hw.memsize", "vm.swapusage"):
            try:
                result[field] = subprocess.run(["sysctl", "-n", field], check=True, capture_output=True,
                                               text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError) as error:
                result[field] = {"error": str(error)}
    elif sys.platform == "linux":
        result.update(system_memory=linux_memory_snapshot(), gpu_devices=gpu_snapshot())
    return result


def main():
    parser = argparse.ArgumentParser(description="Измерить локальную модель на восьми диагностических примерах")
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = os.environ.get("ISTOK_OLLAMA_URL", "http://127.0.0.1:11434")
    if urlsplit(base).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Для этого прогона нужен локальный адрес Ollama.")
    original_request = llm._request
    metadata = original_request("/api/show", {"model": args.model})
    result = {"model_metadata": {key: metadata.get(key) for key in ("details", "parameters", "capabilities", "thinking")},
              "requests": [], "resources": {"before": memory_snapshot(), "samples": []}}
    stop = Event()
    sampler = Thread(target=sample_resources, args=(original_request, result["resources"]["samples"], stop), daemon=True)

    def recorded(path, payload=None, timeout=180):
        return record_request(original_request, result["requests"], path, payload, timeout)

    def progress(item, done, total):
        status = item.get("error") or item["predicted"]
        print(f"[{done}/{total}] {item['example_id']} · {status}", file=sys.stderr, flush=True)

    llm._request = recorded
    sampler.start()
    try:
        result["evaluation"] = evaluation.run(args.model, "test", on_result=progress)
    finally:
        llm._request = original_request
        stop.set()
        sampler.join(timeout=5)
        result["resources"]["sampling_stopped"] = not sampler.is_alive()
        result["resources"]["samples"] = list(result["resources"]["samples"])
        result["resources"]["after"] = memory_snapshot()
        peaks = {}
        for sample in result["resources"]["samples"]:
            for model in sample.get("models", []):
                peak = peaks.setdefault(model["name"], {"size": 0, "size_vram": 0, "context_length": 0})
                for field in peak:
                    peak[field] = max(peak[field], model.get(field) or 0)
        result["resources"]["peaks_by_model"] = peaks
        result["resources"]["interpretation"] = "Показатели size и size_vram сообщает Ollama и относятся к резервированию модели. Linux system_memory отражает видимую ядру общесистемную память. gpu_devices отражает память устройства по данным драйвера, включая использование другими процессами. /proc/meminfo не задаёт лимит контейнера. Снимки делаются примерно раз в секунду."
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.output)
    print(json.dumps(result["evaluation"]["metrics"], ensure_ascii=False, indent=2))
    return int(bool(result["evaluation"]["metrics"]["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
