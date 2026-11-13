"""Private Qwen-only corrector. Deploy explicitly; never downloads models on Mac."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen

from plagiarism.ai_proofreading import request_payload, parse_response

ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = Path('/opt/istok')
APP = 'istok-corrector'
IMAGE = 'ollama/ollama:0.34.2@sha256:c715bebf769913db6c82d96f8a8dfee989c4bdfe19fa7d700e3d41ee0ceb5461'
MODEL = 'qwen3.5:9b'
DIGEST = '56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90'
FILES = ('cloud/modal_corrector.py', 'plagiarism/ai_proofreading.py')
app = None
QwenCorrector = None


def source_manifest(root=ROOT):
    root = Path(root).resolve()
    files = []
    for name in FILES:
        path = root / name
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('Corrector upload must contain only allowlisted ordinary source files')
        body = path.read_bytes()
        files.append({'path': name, 'sha256': sha256(body).hexdigest(), 'bytes': len(body)})
    return files


def deployment_id(manifest):
    return sha256(json.dumps({'files': manifest, 'image': IMAGE, 'model': MODEL,
                             'digest': DIGEST}, sort_keys=True).encode()).hexdigest()


def loopback(path, payload=None, timeout=170):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    request = Request('http://127.0.0.1:11434' + path, data=data, headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise RuntimeError('Model response too large')
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get('error'):
        raise RuntimeError('Model did not return an answer')
    return result


class _Core:
    def _start(self):
        self._server = None
        manifest = json.loads(os.environ['ISTOK_CORRECTOR_UPLOADS'])
        if source_manifest(REMOTE_ROOT) != manifest:
            raise RuntimeError('Corrector source version mismatch')
        self._identity = deployment_id(manifest)
        # Ephemeral weights avoid recurring Volume storage charges. No mounted data.
        environment = {**os.environ, 'OLLAMA_HOST': '127.0.0.1:11434', 'OLLAMA_MODELS': '/tmp/istok-qwen',
                       'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NUM_PARALLEL': '1', 'OLLAMA_MAX_LOADED_MODELS': '1',
                       'OLLAMA_FLASH_ATTENTION': '1', 'OLLAMA_KV_CACHE_TYPE': 'q8_0'}
        self._server = subprocess.Popen(['ollama', 'serve'], env=environment,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if self._server.poll() is not None:
                    raise RuntimeError('Corrector failed to start')
                try:
                    loopback('/api/tags', timeout=2)
                    break
                except (OSError, ValueError):
                    time.sleep(.5)
            else:
                raise RuntimeError('Corrector startup timed out')
            subprocess.run(['ollama', 'pull', MODEL], env=environment, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
            installed = loopback('/api/tags', timeout=5).get('models', [])
            if {item['name']: item['digest'] for item in installed} != {MODEL: DIGEST}:
                raise RuntimeError('Qwen digest changed; refusing inference')
        except BaseException:
            self._stop()
            raise

    def suggest(self, text, expected_deployment_id):
        if expected_deployment_id != self._identity:
            raise RuntimeError('Corrector deployment mismatch')
        payload = request_payload(text, MODEL)
        response = loopback('/api/chat', payload, timeout=170)
        return {'deployment_id': self._identity, 'model_digest': DIGEST,
                'result': parse_response(text, response)}

    def _stop(self):
        process = getattr(self, '_server', None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def build_service(manifest):
    import modal
    global app, QwenCorrector
    image = modal.Image.from_registry(IMAGE, add_python='3.11').entrypoint([]).env({
        'PYTHONPATH': str(REMOTE_ROOT), 'ISTOK_CORRECTOR_UPLOADS': json.dumps(manifest),
        'ISTOK_CORRECTOR_BOOTSTRAP': '1',
    })
    for entry in manifest:
        image = image.add_local_file(ROOT / entry['path'], str(REMOTE_ROOT / entry['path']), copy=True)
    app = modal.App(APP, include_source=False)
    implementation = type('QwenCorrector', (_Core,), {
        '__module__': 'cloud.modal_corrector',
        '_start': modal.enter()(_Core._start), 'suggest': modal.method()(_Core.suggest),
        '_stop': modal.exit()(_Core._stop),
    })
    QwenCorrector = app.cls(image=image, gpu='L40S', cpu=4, memory=32768,
        min_containers=0, max_containers=1, buffer_containers=0, scaledown_window=30,
        timeout=180, startup_timeout=360, retries=0, include_source=False)(implementation)
    return app, QwenCorrector


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deploy', action='store_true')
    args = parser.parse_args()
    manifest = source_manifest()
    config = {'app': APP, 'class_name': 'QwenCorrector', 'deployment_id': deployment_id(manifest),
              'model_digest': DIGEST, 'files': manifest, 'gpu': 'L40S', 'persistent_weights': False,
              'max_containers': 1, 'startup_timeout': 360, 'timeout': 180, 'idle_seconds': 30,
              'public_endpoint': False}
    if args.deploy:
        import modal
        application, service = build_service(manifest)
        with modal.enable_output():
            application.deploy(environment_name='main')
        config['class_id'] = service.object_id
        path = ROOT / '.runtime/corrector-deployment.json'
        path.write_text(json.dumps(config, indent=2) + '\n')
        path.chmod(0o600)
    print(json.dumps(config, indent=2))


if os.environ.get('ISTOK_CORRECTOR_BOOTSTRAP') == '1':
    build_service(json.loads(os.environ['ISTOK_CORRECTOR_UPLOADS']))

if __name__ == '__main__':
    from cloud.modal_corrector import main as canonical_main
    canonical_main()
