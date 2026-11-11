"""Private, idle-scaled Ollama gateway. Importing this module does not deploy it."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from plagiarism import cloud_contract


ROOT = Path(__file__).resolve().parent.parent
REMOTE_ROOT = Path('/opt/istok')
IMAGE = 'ollama/ollama:0.34.2@sha256:c715bebf769913db6c82d96f8a8dfee989c4bdfe19fa7d700e3d41ee0ceb5461'
FILES = ('cloud/modal_inference.py', 'plagiarism/cloud_contract.py')
DEFAULT_CONFIG = ROOT / '.runtime/cloud-models.json'
app = None
OllamaService = None


def source_manifest(root=ROOT):
    root = Path(root).resolve()
    entries = []
    for name in FILES:
        path = root / name
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise ValueError('Нужны обычные файлы из списка отправки сервиса.')
        if not path.resolve().is_relative_to(root):
            raise ValueError('Файл сервиса находится вне проекта.')
        data = path.read_bytes()
        entries.append({'path': name, 'sha256': sha256(data).hexdigest(), 'bytes': len(data)})
    return entries


def loopback_request(path, payload=None, timeout=180):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    request = Request('http://127.0.0.1:11434' + path, data=data,
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(cloud_contract.MAX_RESPONSE_BYTES + 1)
    if len(raw) > cloud_contract.MAX_RESPONSE_BYTES:
        raise RuntimeError('Ответ модели превышает допустимый размер.')
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get('error'):
        raise RuntimeError('Серверная модель не смогла выполнить запрос.')
    return result


class _ServiceCore:
    def _start(self):
        self._server = None
        self._config = cloud_contract.validate_manifest(json.loads(os.environ['ISTOK_SERVICE_MANIFEST']))
        expected = json.loads(os.environ['ISTOK_SERVICE_UPLOADS'])
        if source_manifest(REMOTE_ROOT) != expected:
            raise RuntimeError('Файлы сервиса не совпадают с проверенной версией.')
        models = Path('/tmp/istok-models')
        models.mkdir(parents=True, exist_ok=True)
        environment = {**os.environ, 'OLLAMA_HOST': '127.0.0.1:11434', 'OLLAMA_MODELS': str(models),
                       'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NUM_PARALLEL': '1', 'OLLAMA_MAX_LOADED_MODELS': '2',
                       'OLLAMA_FLASH_ATTENTION': '1', 'OLLAMA_KV_CACHE_TYPE': 'q8_0'}
        self._server = subprocess.Popen(['ollama', 'serve'], env=environment,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self._server.poll() is not None:
                    raise RuntimeError('Сервис модели не запустился.')
                try:
                    loopback_request('/api/tags', timeout=2)
                    break
                except (OSError, ValueError):
                    time.sleep(0.5)
            else:
                raise RuntimeError('Истекло время подготовки сервиса модели.')
            for entry in self._config['models']:
                subprocess.run(['ollama', 'pull', entry['name']], env=environment, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=450)
            installed = loopback_request('/api/tags', timeout=5).get('models', [])
            self._digests = {entry['name']: entry['digest'] for entry in installed}
            if self._digests != cloud_contract.model_digests(self._config):
                raise RuntimeError('Изменились контрольные суммы моделей. Запуск остановлен.')
        except BaseException:
            self._stop()
            raise

    def request(self, path, payload, expected_deployment_id):
        if expected_deployment_id != self._config['deployment_id']:
            raise RuntimeError('Версия сервиса не соответствует клиенту.')
        checked = cloud_contract.validate_request(path, payload, self._config)
        response = loopback_request(path, checked, timeout=170)
        return {'deployment_id': self._config['deployment_id'], 'model_digests': self._digests,
                'response': response}

    def _stop(self):
        process = getattr(self, '_server', None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def build_service(config, uploads):
    """Define a private Class locally. Deployment is an explicit separate operation."""
    import modal

    global app, OllamaService
    config = cloud_contract.validate_manifest(config)
    image = modal.Image.from_registry(IMAGE, add_python='3.11').entrypoint([]).env({
        'PYTHONPATH': str(REMOTE_ROOT),
        'ISTOK_SERVICE_MANIFEST': json.dumps(config, ensure_ascii=False, separators=(',', ':')),
        'ISTOK_SERVICE_UPLOADS': json.dumps(uploads, ensure_ascii=False, separators=(',', ':')),
        'ISTOK_SERVICE_BOOTSTRAP': '1',
    })
    for entry in uploads:
        image = image.add_local_file(ROOT / entry['path'], str(REMOTE_ROOT / entry['path']), copy=True)
    app = modal.App(config['app_name'], include_source=False)
    implementation = type('OllamaService', (_ServiceCore,), {
        '__module__': 'cloud.modal_inference',
        '_start': modal.enter()(_ServiceCore._start),
        'request': modal.method()(_ServiceCore.request),
        '_stop': modal.exit()(_ServiceCore._stop),
    })
    OllamaService = app.cls(image=image, gpu='L40S', cpu=4, memory=32768,
                           min_containers=0, max_containers=1, buffer_containers=0,
                           scaledown_window=60, timeout=180, startup_timeout=600,
                           retries=0, include_source=False)(implementation)
    return app, OllamaService


def config_from_benchmark(path, model, uploads):
    benchmark = json.loads(Path(path).read_text(encoding='utf-8'))
    installed = benchmark['runtime']['installed_models']['models']
    names = (model, 'bge-m3:latest')
    entries = []
    for name, capability in zip(names, ('completion', 'embedding')):
        entry = next((item for item in installed if item['name'] == name), None)
        if entry is None:
            raise ValueError('В проверенном прогоне отсутствует выбранная модель.')
        entries.append({**entry, 'capabilities': [capability]})
    fingerprint = json.dumps({'files': uploads, 'models': entries, 'image': IMAGE},
                             sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    config = {'provider': 'modal', 'app_name': 'istok-inference', 'class_name': 'OllamaService',
              'environment': 'main', 'deployment_id': sha256(fingerprint).hexdigest(),
              'default_model': model, 'embedding_model': 'bge-m3', 'models': entries,
              'verified_at': datetime.now(timezone.utc).isoformat(), 'synthetic_only': True,
              'startup_timeout_seconds': 600, 'image': IMAGE}
    return cloud_contract.validate_manifest(config)


def main(arguments=None):
    parser = argparse.ArgumentParser(description='Подготовить или разместить приватную серверную модель')
    parser.add_argument('--benchmark-resources', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--deploy', action='store_true')
    parser.add_argument('--output', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(arguments)
    uploads = source_manifest()
    config = config_from_benchmark(args.benchmark_resources, args.model, uploads)
    if not args.deploy:
        print(json.dumps({'mode': 'local_plan', 'config': config, 'files': uploads}, ensure_ascii=False, indent=2))
        return 0
    application, service = build_service(config, uploads)
    import modal

    with modal.enable_output():
        application.deploy(environment_name=config['environment'])
    config['class_id'] = service.object_id
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(args.output)
    print(f'Конфигурация сохранена в {args.output}')
    return 0


if os.environ.get('ISTOK_SERVICE_BOOTSTRAP') == '1':
    build_service(json.loads(os.environ['ISTOK_SERVICE_MANIFEST']),
                  json.loads(os.environ['ISTOK_SERVICE_UPLOADS']))

if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    from cloud.modal_inference import main as canonical_main

    raise SystemExit(canonical_main())
