"""Local operator setup, separate from visitor-facing settings and source data."""

import json
import os
from pathlib import Path
import re
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent


def _path(provider="tavily"):
    name = {"tavily":"search-api.json", "serpapi":"serpapi-api.json"}.get(provider)
    if name is None:
        return None
    return Path(os.environ.get("ISTOK_RUNTIME_DIR", ROOT / ".runtime")) / name


def _saved(provider="tavily"):
    path = _path(provider)
    if path is None:
        return {}
    try:
        if path.is_symlink() or path.stat().st_size > 4096 or path.stat().st_mode & 0o077:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("provider") != provider
                or not isinstance(value.get("api_key"), str)):
            return {}
        return value
    except (OSError, ValueError, UnicodeError):
        return {}


def key_for(provider):
    if provider == "serpapi-yandex":
        provider = "serpapi"
    if provider not in {"tavily", "brave", "serpapi"}:
        return ""
    environment = os.environ.get("ISTOK_" + provider.upper() + "_API_KEY", "").strip()
    if environment:
        return environment
    saved = _saved(provider)
    return saved.get("api_key", "") if saved.get("provider") == provider else ""


def selected_provider():
    explicit = os.environ.get("ISTOK_SEARCH_PROVIDER", "")
    if explicit:
        return explicit if explicit in {"tavily", "brave", "serpapi", "serpapi-yandex"} else "unavailable"
    if key_for("tavily"):
        return "tavily"
    return "brave" if key_for("brave") else "serpapi" if key_for("serpapi") else "tavily"


def save_local(provider, api_key):
    if (provider != "tavily" or not isinstance(api_key, str)
            or not re.fullmatch(r"tvly-[A-Za-z0-9_-]{12,240}", api_key.strip())):
        raise ValueError("Введите ключ Tavily из личного кабинета.")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({"provider": provider, "api_key": api_key.strip()}, stream)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"saved": True, "provider": provider}
