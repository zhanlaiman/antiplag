"""Private Modal transport; metadata reads never contact the provider."""

from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import re
from threading import Lock
from uuid import uuid4

from plagiarism.cloud_contract import (CloudContractError, tags_response, validate_envelope,
                                       validate_manifest, validate_request)


ROOT = Path(__file__).resolve().parent.parent
_COMPUTE_LOCK = Lock()


class CloudTransportError(RuntimeError):
    def __init__(self, message, code="cloud_unavailable", details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def _now():
    return datetime.now(timezone.utc).isoformat()


def _runtime():
    return Path(os.environ.get("ISTOK_RUNTIME_DIR", ROOT / ".runtime"))


def load_manifest():
    path = Path(os.environ.get("ISTOK_CLOUD_MANIFEST", _runtime() / "cloud-models.json"))
    try:
        if path.stat().st_size > 100_000:
            raise ValueError("oversized")
        return validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError) as error:
        raise CloudTransportError("Облачная модель не настроена. Нужна проверенная конфигурация сервиса.",
                                  code="cloud_not_configured") from error


def _read_health():
    try:
        path = _runtime() / "cloud-health.json"
        if path.stat().st_size > 10_000:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def status():
    """Configuration readiness, deliberately not a live GPU or auth health check."""
    disabled = (_runtime() / "local-inference-disabled").exists()
    base = {"provider": "modal", "configured": False, "can_submit": False, "available": False,
            "models": [], "embedding_models": [], "embedding_available": False,
            "last_success_at": None, "synthetic_only": True, "local_inference_disabled": disabled,
            "status_kind": "configuration", "capabilities": []}
    try:
        manifest = load_manifest()
    except CloudTransportError as error:
        return base | {"model": "", "embedding_model": "", "error": str(error), "code": error.code}
    health = _read_health()
    same_deployment = health.get("deployment_id") == manifest["deployment_id"]
    models = [item["name"] for item in manifest["models"] if "completion" in item["capabilities"]]
    embeddings = [item["name"] for item in manifest["models"] if "embedding" in item["capabilities"]]
    return base | {"configured": True, "can_submit": True, "available": True,
                   "model": manifest["default_model"], "models": models,
                   "embedding_model": manifest["embedding_model"], "embedding_models": embeddings,
                   "embedding_available": True, "synthetic_only": manifest["synthetic_only"],
                   "deployment_id": manifest["deployment_id"], "verified_at": manifest["verified_at"],
                   "capabilities": ["judge", "rewrite", "embed"],
                   "last_success_at": health.get("last_success_at") if same_deployment else None,
                   "last_error_code": health.get("last_error_code") if same_deployment else None,
                   "error": None}


def _service(manifest):
    # Importing the SDK is deliberately postponed until an explicit compute call.
    os.environ.setdefault("MODAL_CONFIG_PATH", str(_runtime() / "modal-config.toml"))
    modal = importlib.import_module("modal")
    service_class = modal.Cls.from_name(manifest["app_name"], manifest["class_name"],
                                        environment_name=manifest["environment"])
    if manifest.get("class_id"):
        # This control-plane lookup happens only during an explicit compute request.
        service_class.hydrate()
        if service_class.object_id != manifest["class_id"]:
            raise CloudTransportError("Облачный сервис обновлён. Нужна проверенная конфигурация новой версии.",
                                      code="cloud_deployment_changed")
    return service_class()


def request(path, payload=None, timeout=180):
    manifest = load_manifest()
    if path == "/api/tags" and payload is None:
        return tags_response(manifest)
    try:
        validated = validate_request(path, payload, manifest)
    except CloudContractError as error:
        raise CloudTransportError(str(error), code="cloud_request_rejected") from error
    if type(timeout) not in (int, float) or not 0 < timeout <= 600:
        raise CloudTransportError("Некорректное время ожидания модели.", code="cloud_request_rejected")
    with _COMPUTE_LOCK:
        call = None
        record_path = _runtime() / "cloud-calls" / (uuid4().hex + ".json")
        record = {"deployment_id": manifest["deployment_id"], "path": path,
                  "model": validated["model"], "started_at": _now(), "status": "starting"}
        # Fail before remote submission if the call cannot be recorded locally.
        try:
            _write_json(record_path, record)
        except OSError as error:
            raise CloudTransportError("Не удалось сохранить идентификатор облачного запроса.",
                                      code="cloud_tracking_failed") from error
        try:
            call = _service(manifest).request.spawn(path, validated, manifest["deployment_id"])
            call_id = getattr(call, "object_id", "")
            if not isinstance(call_id, str) or not re.fullmatch(r"fc-[A-Za-z0-9]+", call_id):
                raise CloudTransportError("Сервис не вернул идентификатор запроса.", code="cloud_tracking_failed")
            record.update(call_id=call_id, status="running")
            _write_json(record_path, record)
            # Startup includes downloading ephemeral weights. It is not an Ollama timeout.
            envelope = call.get(timeout=600 + timeout)
            response = validate_envelope(envelope, manifest)
        except BaseException as error:
            cancellation = None
            if call is not None:
                try:
                    call.cancel()
                    cancellation = "requested"
                except Exception:
                    cancellation = "failed"
            is_timeout = isinstance(error, TimeoutError) or type(error).__name__ in {"FunctionTimeoutError", "TimeoutError"}
            if isinstance(error, CloudTransportError):
                failure = error
            elif isinstance(error, CloudContractError):
                failure = CloudTransportError(str(error), code="cloud_response_rejected")
            elif is_timeout:
                failure = CloudTransportError("Время ожидания облачной проверки истекло. Отправлен запрос остановки.",
                                              code="cloud_timeout")
            else:
                failure = CloudTransportError("Облачная модель недоступна. Проверьте доступ и лимиты сервиса.",
                                              code="cloud_unavailable")
            if call is not None and failure.details is None:
                failure.details = {"call_id": record.get("call_id"), "cancellation": cancellation}
            if cancellation == "failed" and is_timeout:
                failure = CloudTransportError("Время ожидания истекло. Остановку запроса не удалось подтвердить; действует серверный предел времени.",
                                              code="cloud_timeout", details=failure.details)
            record.update(status="failed", finished_at=_now(), code=failure.code, cancellation=cancellation)
            try:
                _write_json(record_path, record)
                health = _read_health()
                _write_json(_runtime() / "cloud-health.json", {"deployment_id": manifest["deployment_id"],
                            "last_success_at": health.get("last_success_at")
                            if health.get("deployment_id") == manifest["deployment_id"] else None,
                            "last_error_code": failure.code})
            except OSError:
                pass
            if not isinstance(error, Exception):
                raise
            raise failure from None
        record.update(status="complete", finished_at=_now())
        try:
            _write_json(record_path, record)
            _write_json(_runtime() / "cloud-health.json", {"deployment_id": manifest["deployment_id"],
                        "last_success_at": record["finished_at"], "last_error_code": None})
        except OSError:
            # Do not lose a completed generation or repeat it because metadata saving failed.
            pass
        return response
