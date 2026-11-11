"""Shared, dependency-free contract for the private inference gateway."""

from copy import deepcopy
from datetime import datetime
import json
import re


MAX_REQUEST_BYTES = 600_000
MAX_RESPONSE_BYTES = 2_000_000


class CloudContractError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise CloudContractError(message)


def _json_size(value, maximum):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as error:
        raise CloudContractError("Некорректные данные облачного запроса.") from error
    _require(len(encoded) <= maximum, "Облачный запрос или ответ превышает допустимый размер.")


def model_entry(manifest, name):
    for entry in manifest["models"]:
        if name == entry["name"] or (":" not in name and name + ":latest" == entry["name"]):
            return entry
    raise CloudContractError("Эта модель не разрешена в облачной конфигурации.")


def model_digests(manifest):
    return {entry["name"]: entry["digest"] for entry in manifest["models"]}


def validate_manifest(config):
    _require(isinstance(config, dict), "Не найдена корректная облачная конфигурация.")
    _require(config.get("provider") == "modal", "Неизвестный облачный провайдер.")
    for key in ("app_name", "class_name", "environment"):
        value = config.get(key)
        _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value),
                 "Некорректное имя облачного сервиса.")
    _require(isinstance(config.get("deployment_id"), str)
             and re.fullmatch(r"[a-f0-9]{64}", config["deployment_id"]), "Некорректная версия облачного сервиса.")
    if "class_id" in config:
        _require(isinstance(config["class_id"], str) and re.fullmatch(r"cs-[A-Za-z0-9]+", config["class_id"]),
                 "Некорректный идентификатор облачного класса.")
    _require(type(config.get("synthetic_only")) is bool, "Не указан режим облачной проверки.")
    try:
        verified = datetime.fromisoformat(config["verified_at"].replace("Z", "+00:00"))
        _require(verified.tzinfo is not None, "Время проверки конфигурации должно содержать часовой пояс.")
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise CloudContractError("Некорректное время проверки облачной конфигурации.") from error
    entries = config.get("models")
    _require(isinstance(entries, list) and len(entries) == 2, "Нужны одна модель текста и одна модель поиска.")
    names = set()
    for entry in entries:
        _require(isinstance(entry, dict), "Некорректная запись облачной модели.")
        name = entry.get("name")
        _require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", name)
                 and name not in names, "Некорректное или повторяющееся имя облачной модели.")
        names.add(name)
        _require(isinstance(entry.get("digest"), str) and re.fullmatch(r"[a-f0-9]{64}", entry["digest"]),
                 "Нужна проверенная контрольная сумма модели.")
        caps = entry.get("capabilities")
        _require(isinstance(caps, list) and caps and all(isinstance(item, str) for item in caps),
                 "Не указаны возможности облачной модели.")
    for key, capability in (("default_model", "completion"), ("embedding_model", "embedding")):
        name = config.get(key)
        _require(isinstance(name, str) and name, "Не выбрана облачная модель.")
        _require(capability in model_entry(config, name)["capabilities"], "Модель не поддерживает выбранную операцию.")
    _require(model_entry(config, config["default_model"])["name"]
             != model_entry(config, config["embedding_model"])["name"], "Нужны две разные облачные модели.")
    _json_size(config, 100_000)
    return deepcopy(config)


def tags_response(manifest):
    return {"models": deepcopy(manifest["models"])}


def validate_request(path, payload, manifest):
    _require(path in {"/api/chat", "/api/embed"}, "Эта операция облачной модели запрещена.")
    _require(isinstance(payload, dict), "Нужен объект облачного запроса.")
    _json_size(payload, MAX_REQUEST_BYTES)
    name = payload.get("model")
    _require(isinstance(name, str), "Не выбрана облачная модель.")
    entry = model_entry(manifest, name)
    if path == "/api/chat":
        _require(entry["name"] == model_entry(manifest, manifest["default_model"])["name"],
                 "Эта модель не разрешена для сравнения текста.")
        _require(set(payload) <= {"model", "messages", "format", "stream", "think", "options", "keep_alive"},
                 "Запрос содержит запрещённые параметры.")
        messages = payload.get("messages")
        _require(isinstance(messages, list) and len(messages) == 2, "Некорректные сообщения для модели.")
        for item, role in zip(messages, ("system", "user")):
            _require(isinstance(item, dict) and set(item) == {"role", "content"}
                     and item.get("role") == role and isinstance(item.get("content"), str)
                     and 0 < len(item["content"]) <= 100_000, "Некорректные сообщения для модели.")
        _require(payload.get("stream") is False and payload.get("think") is False,
                 "Потоковая генерация и скрытое рассуждение запрещены.")
        _require(isinstance(payload.get("format"), dict), "Нужна JSON-схема результата.")
        options = payload.get("options")
        _require(isinstance(options, dict) and set(options) <= {"temperature", "num_ctx", "num_predict"}
                 and options.get("temperature") == 0 and type(options.get("num_ctx")) is int
                 and 1 <= options["num_ctx"] <= 8192 and type(options.get("num_predict")) is int
                 and 1 <= options["num_predict"] <= 1000, "Некорректные ограничения генерации.")
    else:
        _require(entry["name"] == model_entry(manifest, manifest["embedding_model"])["name"],
                 "Эта модель не разрешена для поиска.")
        _require(set(payload) <= {"model", "input", "truncate", "keep_alive"}, "Запрос содержит запрещённые параметры.")
        texts = payload.get("input")
        _require(isinstance(texts, list) and 1 <= len(texts) <= 32
                 and all(isinstance(item, str) and item.strip() and len(item) <= 12000 for item in texts),
                 "Некорректные фрагменты для поиска.")
        _require(sum(map(len, texts)) <= 120000 and payload.get("truncate") is False,
                 "Фрагменты для поиска превышают допустимый размер.")
    _require(payload.get("keep_alive") in {"0", "0s", "2m", "5m"}, "Некорректное время удержания модели.")
    result = deepcopy(payload)
    result["model"] = entry["name"]
    return result


def validate_envelope(envelope, manifest):
    _require(isinstance(envelope, dict) and envelope.get("deployment_id") == manifest["deployment_id"],
             "Версия облачного сервиса не совпадает с проверенной конфигурацией.")
    _require(envelope.get("model_digests") == model_digests(manifest),
             "Контрольные суммы облачных моделей изменились. Проверка остановлена.")
    response = envelope.get("response")
    _require(isinstance(response, dict), "Облачная модель вернула некорректный ответ.")
    _json_size(response, MAX_RESPONSE_BYTES)
    _require(not response.get("error"), "Облачная модель не смогла выполнить запрос.")
    return response
